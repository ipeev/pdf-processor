import test from 'node:test'
import assert from 'node:assert/strict'
import { routeFor } from '../src/catalogue.ts'
import { parseSearch, parseMatches, highlightedSegments } from '../src/search.ts'

const ID = 'a'.repeat(64)
const RUN = '11111111-1111-4111-8111-111111111111'
const passage = {run_id: RUN,parser:'pypdf',page:5,passage_index:0,snippet:'<mark>neural</mark> network',score:11.5}

test('search is a first-class navigation route',()=>{
  assert.deepEqual(routeFor('/search'),{view:'search'})
  assert.deepEqual(routeFor('/search/unexpected'),{view:'missing'})
})
test('search contracts enforce canonical PDFs and bounded passages',()=>{
  const page = parseSearch({items:[{document_id:ID,filename:'paper.pdf',score:11.5,matching_passages:3,passages:[passage]}],has_more:false,offset:0,total_documents:null})
  assert.equal(page.items[0].passages[0].page,5)
  assert.throws(()=>parseSearch({items:[...page.items,...page.items],has_more:false,offset:0,total_documents:null}))
  assert.throws(()=>parseSearch({items:[],has_more:true,offset:0,total_documents:999}))
  assert.equal(parseMatches({items:[passage],total:3,has_more:true}).total,3)
})
test('OpenSearch highlighting is parsed into plain React content, never raw HTML',()=>{
  const parts=highlightedSegments('<mark>network</mark> <script>alert(1)</script>')
  assert.deepEqual(parts,[{text:'network',highlighted:true},{text:' <script>alert(1)</script>',highlighted:false}])
})
