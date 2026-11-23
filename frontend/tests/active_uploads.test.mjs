import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { isActiveRun, onlyActiveRuns, readActiveUploadIds, writeActiveUploadIds } from '../src/activeUploads.ts'

const id1 = 'a2e8e817-273d-4e21-b3bc-4cc0919b8352'
const id2 = 'f9b56c42-0e9d-4a9d-91cc-42d13517ab0e'
const src = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
function memoryStorage(initial = null) {
  let contents = initial
  return {
    getItem: () => contents,
    setItem: (_key, value) => { contents = value },
    read: () => contents,
  }
}
test('Upload contains only queued, processing and summarizing; all terminal statuses leave for History', () => {
  assert.deepEqual(['queued', 'processing', 'summarizing', 'completed', 'partial', 'failed'].map(isActiveRun),
    [true, true, true, false, false, false])
  const initial = ['queued', 'processing', 'summarizing', 'completed', 'partial', 'failed'].map((status, i) => ({status, i}))
  assert.deepEqual(onlyActiveRuns(initial).map(item=>item.i), [0,1,2])
  assert.equal(onlyActiveRuns([]).length, 0)
  assert.deepEqual(initial.map(item=>item.status), ['queued','processing','summarizing','completed','partial','failed'])
})
test('Active run IDs survive browser navigation/reload without persisting PDFs or filenames', () => {
  const storage = memoryStorage()
  writeActiveUploadIds(storage, [id1, id2, id1, 'unsafe'])
  assert.deepEqual(readActiveUploadIds(storage), [id1, id2])
  assert.deepEqual(JSON.parse(storage.read()), [id1, id2])
  const invalid = memoryStorage('not JSON')
  assert.deepEqual(readActiveUploadIds(invalid), [])
  const unavailable = { getItem: () => { throw Error('storage disabled') }, setItem: () => { throw Error('storage disabled') } }
  assert.deepEqual(readActiveUploadIds(unavailable), [])
  assert.doesNotThrow(() => writeActiveUploadIds(unavailable, [id1]))
})
test('Upload workspace stays mounted during History/Library navigation and polling evicts terminal runs', () => {
  assert.match(src, /<div hidden=\{route\.view !== 'upload'\}><Workspace\/>/)
  assert.doesNotMatch(src, /route\.view === 'upload' \? <Workspace key=\{path\}/)
  assert.match(src, /return runId \? updated : onlyActiveRuns\(updated\)/)
  assert.match(src, /setJobs\(previous => onlyActiveRuns\(\[/)
  assert.match(src, /readActiveUploadIds\(storage\)/)
  assert.match(src, /writeActiveUploadIds\(window\.sessionStorage, jobs\.map\(job => job\.id\)\)/)
  assert.match(src, /No active processing jobs/)
  assert.match(src, /Completed, partial, and failed runs are available in Processing History/)
})
