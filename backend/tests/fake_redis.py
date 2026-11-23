"""In-memory async Redis test double; NOT a replacement for real Redis integration tests."""
from copy import deepcopy
from fnmatch import fnmatch
import time
import json
from redis.exceptions import ResponseError, WatchError, ConnectionError as RedisConnectionError


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.actions = []
        self.watched = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def watch(self, *keys):
        self.watched.update({key: self.redis._versions.get(key, 0) for key in keys})

    async def hget(self, key, field):
        return await self.redis.hget(key, field)

    async def get(self, key):
        return await self.redis.get(key)

    def multi(self):
        return self

    def __getattr__(self, name):
        def schedule(*args, **kwargs):
            self.actions.append((name, args, kwargs))
            return self
        return schedule

    async def execute(self, raise_on_error=True):
        if any(self.redis._versions.get(key, 0) != version
               for key, version in self.watched.items()):
            raise WatchError("watched key changed")
        replies = []
        for name, args, kwargs in self.actions:
            try:
                replies.append(await getattr(self.redis, name)(*args, **kwargs))
            except Exception as exc:
                if raise_on_error:
                    raise
                replies.append(exc)
        if self.redis.fail_exec_after_commit:
            self.redis.fail_exec_after_commit = False
            raise ConnectionError("simulated lost EXEC reply")
        return replies


class FakeRedis:
    def __init__(self):
        self.keys = {}
        self.streams = {}
        self.groups = {}
        self.fail_exec_after_commit = False
        self.available = True
        self._sequence = 0
        self.expiry = {}
        self._versions = {}
        self.clock = time.monotonic

    def _touch(self, key):
        self._versions[key] = self._versions.get(key, 0) + 1

    def _expire_key(self, key):
        deadline = self.expiry.get(key)
        if deadline is not None and self.clock() >= deadline:
            self.keys.pop(key, None)
            self.expiry.pop(key, None)

    async def ping(self):
        if not self.available:
            raise RedisConnectionError("Redis disconnected")
        return True

    async def aclose(self):
        return None

    async def info(self, section=None):
        if not self.available:
            raise RedisConnectionError("Redis disconnected")
        return {"used_memory": 8192 + len(self.keys) * 64, "maxmemory": 256 * 1024 * 1024}

    async def dbsize(self):
        if not self.available:
            raise RedisConnectionError("Redis disconnected")
        for key in list(self.keys):
            self._expire_key(key)
        return len(set(self.keys) | set(self.streams))

    async def scan(self, cursor=0, match=None, count=10):
        if not self.available:
            raise RedisConnectionError("Redis disconnected")
        for key in list(self.keys):
            self._expire_key(key)
        keys = [key for key in self.keys if match is None or fnmatch(key, match)]
        return (0, sorted(keys))

    async def xinfo_groups(self, stream):
        if not self.available:
            raise RedisConnectionError("Redis disconnected")
        if stream not in self.streams:
            raise ResponseError("ERR no such key")
        result = []
        for (s, name), group in self.groups.items():
            if s != stream:
                continue
            result.append({"name": name, "lag": sum(item is not None for item in self.streams[stream][group["last"] + 1:]),
                           "pending": len(group["pel"])})
        return result

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    async def hset(self, key, mapping=None, **kwargs):
        self._expire_key(key)
        if key in self.keys and not isinstance(self.keys[key], dict):
            raise ResponseError("WRONGTYPE")
        self.keys.setdefault(key, {}).update({k: str(v) for k, v in (mapping or {}).items()})
        self._touch(key)
        return len(mapping or {})

    async def hget(self, key, field):
        return (await self.hgetall(key)).get(field)

    async def hgetall(self, key):
        self._expire_key(key)
        value = self.keys.get(key, {})
        if not isinstance(value, dict):
            raise ResponseError("WRONGTYPE")
        return deepcopy(value)

    async def hincrby(self, key, field, amount):
        current = await self.hgetall(key)
        next_value = int(current.get(field, "0")) + amount
        await self.hset(key, mapping={field: str(next_value)})
        return next_value

    async def set(self, key, value, ex=None):
        self.keys[key] = value if isinstance(value, bytes) else str(value)
        self._touch(key)
        if ex is not None:
            self.expiry[key] = self.clock() + int(ex)
        else:
            self.expiry.pop(key, None)
        return True

    async def get(self, key):
        self._expire_key(key)
        return self.keys.get(key)

    async def mget(self, *keys):
        return [await self.get(key) for key in keys]

    async def expire(self, key, seconds):
        self._expire_key(key)
        if key not in self.keys:
            return False
        self.expiry[key] = self.clock() + int(seconds)
        return True

    async def ttl(self, key):
        self._expire_key(key)
        if key not in self.keys:
            return -2
        if key not in self.expiry:
            return -1
        return max(0, int(self.expiry[key] - self.clock()))

    async def delete(self, *keys):
        count = 0
        for key in keys:
            self._expire_key(key)
            if key in self.keys:
                self.keys.pop(key)
                self._touch(key)
                self.expiry.pop(key, None)
                count += 1
        return count


    async def eval(self, script, numkeys, *args):
        if '-- cache:register' in script:
            assert numkeys == 6
            result, active, meta_key, pages_key, summary_key, stream = args[:6]
            raw_meta, doc_id, parser, result_ttl, active_ttl, digest, prefix, traceparent, upload_id = args[6:]
            meta = json.loads(raw_meta)
            if await self.hgetall(meta_key):
                raise ResponseError('Duplicate document ID')
            snapshot = await self.get(result)
            if snapshot is not None:
                data = json.loads(snapshot)
                meta.update(status='completed', page_count=str(data['page_count']), cache='hit')
                await self.hset(meta_key, mapping=meta)
                await self.set(pages_key, data['pages'], ex=result_ttl)
                await self.set(summary_key, data['summary'], ex=result_ttl)
                if meta.get('archive_document_id'):
                    await self.hset(meta_key, mapping={'status': 'queued', 'cache_source_id': data.get('source_run_id', '')})
                    await self.xadd(stream, {'document_id': doc_id, 'parser': parser, 'action': 'cache_hit', 'traceparent': traceparent, 'upload_id': upload_id})
                else:
                    await self.expire(meta_key, result_ttl)
                outcome = 2
            else:
                owner = await self.get(active)
                owner_meta = await self.hgetall(f'{prefix}:{owner}:meta') if owner else {}
                if owner_meta.get('status') in {'queued', 'processing', 'summarizing'}:
                    meta.update(cache='shared', cache_source_id=owner)
                    await self.hset(meta_key, mapping=meta)
                    if meta.get('archive_document_id'):
                        await self.xadd(stream, {'document_id': doc_id, 'parser': parser, 'action': 'shared', 'traceparent': traceparent, 'upload_id': upload_id})
                    else:
                        await self.expire(meta_key, active_ttl)
                    outcome = 1
                else:
                    meta.update(cache='miss', cache_key=digest)
                    await self.hset(meta_key, mapping=meta)
                    await self.xadd(stream, {'document_id': doc_id, 'parser': parser, 'traceparent': traceparent, 'upload_id': upload_id})
                    await self.set(active, doc_id, ex=active_ttl)
                    outcome = 0
            if self.fail_exec_after_commit:
                self.fail_exec_after_commit = False
                raise RedisConnectionError('simulated lost Redis reply after registration')
            return outcome
        if '-- cache:refresh' in script:
            assert numkeys == 1
            key, doc_id, ttl = args
            return await self.expire(key, ttl) if await self.get(key) == doc_id else 0
        if '-- queue:begin' in script:
            stream, meta_key, group, message_id, consumer, token = args
            pending = self.groups[(stream, group)]["pel"].get(message_id)
            if not pending or pending["owner"] != consumer:
                return 0
            if not await self.hgetall(meta_key):
                return -1
            await self.hset(meta_key, mapping={"owner_token": token})
            return 1
        if '-- queue:heartbeat' in script:
            stream, group, message_id, consumer = args
            pending = self.groups[(stream, group)]["pel"].get(message_id)
            if not pending or pending["owner"] != consumer:
                return 0
            pending["last_ms"] = self.clock() * 1000
            return 1
        if '-- queue:ack_owned' in script:
            stream, meta_key, group, message_id, consumer, token = args
            pending = self.groups[(stream, group)]["pel"].get(message_id)
            if not pending or pending["owner"] != consumer:
                return 0
            if await self.hget(meta_key, "owner_token") != token:
                return 0
            acked = await self.xack(stream, group, message_id)
            if acked:
                await self.xdel(stream, message_id)
                if await self.hget(meta_key, 'archive_document_id'):
                    self.keys[meta_key].pop('owner_token', None)
                    self._touch(meta_key)
            return acked
        if '-- archive:retry_summary' in script:
            meta_key, stream, raw_meta, document_id, traceparent = args
            existing = await self.hgetall(meta_key)
            if (existing.get('status') and existing['status'] != 'partial') or existing.get('owner_token'):
                return 0
            meta = json.loads(raw_meta)
            await self.delete(meta_key)
            await self.hset(meta_key, mapping=meta)
            await self.xadd(stream, {'document_id': document_id, 'parser': meta['parser'], 'action': 'summary',
                                     'traceparent': traceparent, 'upload_id': meta['upload_id']})
            return 1
        # Emulates the atomic summary-only retry script.
        assert numkeys == 3 and "'partial'" in script and "'action', 'summary'" in script
        meta_key, pages_key, stream, document_id, updated, traceparent = args
        meta = await self.hgetall(meta_key)
        if not meta:
            return 0
        if meta.get("status") != "partial":
            return 1
        if await self.get(pages_key) is None:
            return 2
        await self.xadd(stream, {"document_id": document_id,
                                 "parser": meta["parser"], "action": "summary", "traceparent": traceparent, "upload_id": meta.get("upload_id", "")})
        await self.hset(meta_key, mapping={"status": "summarizing", "updated_at": updated,
                                       "owner_token": "", "worker_attempts": "0", "error_code": "", "error_message": ""})
        return 3

    async def xgroup_create(self, stream, group, id="0", mkstream=False):
        if (stream, group) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        if mkstream:
            self.streams.setdefault(stream, [])
        self.groups[(stream, group)] = {"last": -1, "pel": {}}
        return True

    async def xadd(self, stream, fields):
        self._sequence += 1
        entry_id = f"1000-{self._sequence}"
        self.streams.setdefault(stream, []).append((entry_id, fields.copy()))
        return entry_id

    async def xreadgroup(self, group, consumer, streams, count=1, block=None):
        stream, cursor = next(iter(streams.items()))
        data = self.groups[(stream, group)]
        if cursor == ">":
            index = data["last"] + 1
            if index >= len(self.streams.get(stream, [])):
                return []
            ids = []
            positions = self.streams.get(stream, [])
            while index < len(positions) and len(ids) < count:
                item = positions[index]
                if item is not None:
                    ids.append(item)
                    data["pel"][item[0]] = {"owner": consumer, "last_ms": self.clock() * 1000}
                data["last"] = index
                index += 1
            return [(stream, deepcopy(ids))] if ids else []
        pending = [(eid, fields) for item in self.streams.get(stream, []) if item is not None
                   for eid, fields in [item]
                   if eid in data["pel"] and data["pel"][eid]["owner"] == consumer]
        return [(stream, deepcopy(pending[:count]))] if pending else []

    async def xautoclaim(self, stream, group, consumer, min_idle_time, start_id="0-0", count=1):
        data = self.groups[(stream, group)]
        claimed = []
        now_ms = self.clock() * 1000
        for item in self.streams.get(stream, []):
            if item is None or len(claimed) >= count:
                continue
            message_id, fields = item
            pending = data["pel"].get(message_id)
            if pending and message_id >= start_id and now_ms - pending["last_ms"] >= min_idle_time:
                pending["owner"] = consumer
                pending["last_ms"] = now_ms
                claimed.append((message_id, deepcopy(fields)))
        return ("0-0", claimed, [])

    async def xack(self, stream, group, message_id):
        return int(self.groups[(stream, group)]["pel"].pop(message_id, None) is not None)

    async def xpending(self, stream, group):
        return {"pending": len(self.groups[(stream, group)]["pel"])}

    async def xdel(self, stream, *message_ids):
        removed = 0
        for i, item in enumerate(self.streams.get(stream, [])):
            if item is not None and item[0] in message_ids:
                self.streams[stream][i] = None
                removed += 1
        return removed

    async def xlen(self, stream):
        return sum(item is not None for item in self.streams.get(stream, []))
