import test from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {parseDocumentStatus} from '../src/contracts.ts'

const id = 'a2e8e817-273d-4e21-b3bc-4cc0919b8352'
const base = {id, filename:'sample.pdf', parser:'vision_figures', status:'processing',
  cache:'miss', page_count:null, summary:null, error:null}
const progress = {page:5, pages_total:21, attempt:2, max_attempts:3}

test('polling accepts the current extraction page and retry count', () => {
  assert.deepEqual(parseDocumentStatus({...base, page_progress:progress}, id).page_progress, progress)
  assert.equal(parseDocumentStatus(base, id).page_progress, null)
})

test('malformed or stale progress is not displayed', () => {
  for (const invalid of [
    {...progress, page:0}, {...progress, page:22}, {...progress, pages_total:0},
    {...progress, attempt:4}, {...progress, attempt:-1},
    {...progress, max_attempts:8}, {...progress, page:'5'},
  ]) assert.throws(()=>parseDocumentStatus({...base, page_progress:invalid},id),/invalid page progress/)
  assert.throws(()=>parseDocumentStatus({...base, status:'summarizing', page_progress:progress},id),/invalid page progress/)
})

test('upload workspace renders live progress, retries and summary transition',()=>{
  const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  assert.match(source, /Extracting page \$\{selected\.pageProgress\.page\} of \$\{selected\.pageProgress\.pages_total\}/)
  assert.match(source, /retry \$\{selected\.pageProgress\.attempt\}/)
  assert.match(source, /pageProgress: found\.doc\.page_progress/)
  assert.match(source, /Generating the document summary/)
})
