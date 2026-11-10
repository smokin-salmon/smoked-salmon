import catalog from './zh-CN.json'

const words: Record<string, string> = catalog
const token = /ZXQ\d+QXZ/g
const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const patterns = Object.entries(words)
  .filter(([source]) => /ZXQ\d+QXZ/.test(source) && /[A-Za-z]{3}/.test(source.replace(token, '')))
  .sort(([a], [b]) => b.replace(token, '').length - a.replace(token, '').length)
  .map(([source, translation]) => ({
    re: new RegExp('^' + source.split(/(ZXQ\d+QXZ)/).map((part) => /^ZXQ\d+QXZ$/.test(part) ? '(.*?)' : escape(part).replace(/\s+/g, '\\s+')).join('') + '$', 's'),
    tokens: [...source.matchAll(/ZXQ\d+QXZ/g)].map((match) => match[0]),
    translation,
  }))

export function t(value: string): string {
  const key = value.trim().replace(/\s+/g, ' ')
  if (words[key]) return words[key]
  for (const pattern of patterns) {
    const match = pattern.re.exec(value.trim())
    if (!match) continue
    const values = Object.fromEntries(pattern.tokens.map((name, i) => [name, match[i + 1] ?? '']))
    return pattern.translation.replace(token, (name) => values[name] ?? name)
  }
  return value
}
