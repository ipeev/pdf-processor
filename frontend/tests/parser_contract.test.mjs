import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as contracts from '../src/contracts.ts'
import { PARSERS } from '../src/ui.ts'
import { parseRun, parseCataloguePage } from '../src/catalogue.ts'
import { modelLabel, parseSystemSnapshot } from '../src/system.ts'

const id = '12345678-1234-1234-1234-123456789abc'
const digest = 'a'.repeat(64)
const stamp = '2026-11-23T05:00:00Z'
const run = {
  run_id:id, document_id:digest, filename:'YOLOv3.pdf', parser:'vision_figures', status:'completed',
  cache:'miss', page_count:6, created_at:stamp, updated_at:stamp, duration_ms:22000,
  extraction_model:'anthropic/claude-haiku-5.5', summary_model:'summary-model',
  application_version:'0.12.6', cost_usd:0.004, has_extraction:true, has_summary:true,
}

test('backend Python parser enumeration stays identical to frontend choices and shared runtime guard', () => {
  const python = readFileSync(new URL('../../backend/app/schemas.py', import.meta.url), 'utf8')
  const block = python.match(/^class ParserChoice\(StrEnum\):\n((?:    [^\n]+\n)+)/m)
  assert.ok(block, 'Backend ParserChoice enum must be available in the release source')
  const ids = [...block[1].matchAll(/^    [a-z0-9_]+\s*=\s*"([a-z0-9_]+)"/gm)].map(m => m[1])
  assert.deepEqual(ids, PARSERS.map(p => p.id), 'Any parser added to the API needs a frontend card AND validator')
  for (const choice of PARSERS) {
    assert.equal(contracts.isParser(choice.id), true)
    assert.equal(choice.disabled, undefined)
  }
  assert.equal(contracts.isParser('unexpected'), false)
})

test('fourth-parser upload, polling, History and library-run projection accept the SAME parser', () => {
  const parser = 'vision_figures'
  const uploaded = contracts.parseUploadResponse({documents:[{id,filename:'YOLOv3.pdf',parser,status:'queued',cache:'miss'}]},1)
  assert.equal(uploaded[0].parser, parser)
  const polled = contracts.parseDocumentStatus({id, filename:'YOLOv3.pdf',parser,
    status:'completed',summary:'Summary.',cache:'miss',page_count:6},id)
  assert.equal(polled.parser, parser)
  assert.equal(parseRun(run).parser, parser)
  assert.equal(parseCataloguePage({items:[run],next_cursor:null},parseRun,x=>x.run_id).items[0].parser,parser)
  assert.equal(modelLabel(parser), 'AI OCR + Pictures')
})

test('system monitor accepts the fourth parser for a running worker', () => {
  const snap = parseSystemSnapshot({generated_at:stamp, api:{status:'online'},
    redis:{status:'online',memory_bytes:1200,memory_limit_bytes:2000,key_count:2,
      queue:{waiting:0,pending:1},workers:[{id:'abcde123',state:'extracting',parser:'vision_figures'}],
      cache:{hits:0,misses:1,shared:0}},
    rustfs:{status:'online',objects:1,bytes:500,truncated:false},
    openrouter:{status:'not_monitored'}})
  assert.equal(snap.redis.workers[0].parser,'vision_figures')
})
