/**
 * Normalize single-line display equations from PDF/AI Markdown for preview only.
 *
 * remark-math v6 parses `$$x^2$$` on one line as *inline* math, even when the
 * line contains nothing else. Technical PDF extraction commonly emits this
 * form, so make the opening/closing delimiters stand on separate lines.
 *
 * Never touch fenced or indented code, regular inline $...$, or surrounding
 * content. Markdown downloads always use the original, unmodified text.
 */
export function normalizeDisplayMath(markdown: string): string {
  const lines = markdown.split('\n')
  const output: string[] = []
  let fence: { marker: string; length: number } | null = null

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line)

    if (marker) {
      const run = marker[1]
      if (fence === null) {
        fence = { marker: run[0], length: run.length }
      } else if (
        run[0] === fence.marker && run.length >= fence.length &&
        line.slice(marker[0].length).trim() === ''
      ) {
        fence = null
      }
      output.push(line)
      continue
    }

    if (fence === null) {
      // A standalone equation, NOT an inline expression or indented code.
      const equation = /^( {0,3})\$\$(.+)\$\$[ \t]*$/.exec(line)
      if (equation && equation[2].trim()) {
        if (output.length && output[output.length - 1].trim()) output.push('')
        output.push(`${equation[1]}$$`, equation[2], `${equation[1]}$$`)
        if (index + 1 < lines.length && lines[index + 1].trim()) output.push('')
        continue
      }
    }

    output.push(line)
  }

  return output.join('\n')
}
