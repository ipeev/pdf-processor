import test from 'node:test'
import assert from 'node:assert/strict'
import { normalizeDisplayMath } from '../src/math.ts'

// In Docker, npm ci installs the real rendering packages before `npm test`.
// An offline source-only checkout has no node_modules; static wiring tests in
// ui.test.mjs still run, and these browser-independent render checks skip.
let render
try {
  const [{ createElement }, { renderToStaticMarkup }, { default: ReactMarkdown },
    { default: remarkGfm }, { default: remarkMath }, { default: rehypeKatex }] =
      await Promise.all([
        import('react'), import('react-dom/server'), import('react-markdown'),
        import('remark-gfm'), import('remark-math'), import('rehype-katex'),
      ])
  render = (markdown) => renderToStaticMarkup(createElement(ReactMarkdown, {
    remarkPlugins: [remarkGfm, remarkMath],
    rehypePlugins: [[rehypeKatex, {
      trust: false, throwOnError: false, maxExpand: 1000, maxSize: 10,
    }]],
    components: { img: ({ alt }) => createElement('span', null, `[Image: ${alt ?? ''}]`) },
  }, normalizeDisplayMath(markdown)))
} catch (error) {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error
}

const skip = render ? false : 'Requires frontend npm dependencies (installed in Docker)'

test('inline $...$ and block $$...$$ are rendered as accessible KaTeX', { skip }, () => {
  const html = render('Distance $x^2+y^2$ and:\n\n$$\\frac{1}{2}$$')
  assert.match(html, /katex/)
  assert.match(html, /katex-display/)
  assert.match(html, /mathml/)
  assert.doesNotMatch(html, /\$\$\\frac/)
})

test('ordinary Unicode mathematical notation remains readable', { skip }, () => {
  const html = render('Positivity δ(p₁(x), p₂(x)) ≥ 0 and L∞-norm')
  assert.match(html, /δ\(p₁\(x\), p₂\(x\)\) ≥ 0/)
})

test('math delimiters inside fenced code are shown as literal source', { skip }, () => {
  const html = render('```text\n$$x^2$$\n```')
  assert.match(html, /\$\$x\^2\$\$/)
  assert.doesNotMatch(html, /katex-display/)
})

test('malformed math does not crash or enable untrusted links/HTML', { skip }, () => {
  const html = render('$$\\unknowncommand$$\n\n<script>alert(1)</script>\n\n$\\href{javascript:alert(1)}{bad}$')
  assert.ok(html.length > 0)
  assert.doesNotMatch(html, /<script|href="javascript:/i)
})

// These checks run offline too (without installing npm/KaTeX).
test('one-line $$...$$ used by PDF extractors is normalized to display math', () => {
  const input = 'ISO formula:\n\n$$\\delta(p_1,p_2)=\\sqrt{\\sum_{i=1}^{n}(p_1-p_2)^2}$$\nNext section'
  const normalized = normalizeDisplayMath(input)
  assert.match(normalized, /ISO formula:\n\n\$\$\n\\delta\(p_1,p_2\)/)
  assert.match(normalized, /\n\$\$\n\nNext section/)
  assert.equal(input.includes('\n$$\\delta'), true, 'download source must stay unchanged')
})

test('standard display blocks, inline math, and fenced or indented code are preserved', () => {
  const input = [
    'Inline $x^2$ and an amount $25 are ordinary text.',
    '', '$$', '\\int_0^1 x^2\\,dx', '$$', '',
    '```markdown', '$$\\frac{1}{2}$$', '```',
    '', '~~~', '$$code$$', '~~~',
    '    $$indented_code$$',
    'Before $$x+1$$ and after',
    '`$$literal$$`',
  ].join('\n')
  assert.equal(normalizeDisplayMath(input), input)
})

test('single-line display math is rendered as display mode', { skip }, () => {
  const html = render('Text before\n\n$$\\frac{1}{2}$$\n\nText after')
  assert.match(html, /katex-display/)
  assert.match(html, /<mfrac>/)
})
