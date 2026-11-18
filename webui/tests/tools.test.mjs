import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createServer } from 'vite'

// Render the real component so missing input attributes cannot bypass the shared theme.
test('report text fields use the shared text-input styling', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    const { default: Tools } = await server.ssrLoadModule('/src/pages/Tools.svelte')
    const { render } = await server.ssrLoadModule('svelte/server')
    const { body } = render(Tools)
    for (const label of ['Torrent ID or permalink', 'Source URL (optional)']) {
      const input = body.slice(body.indexOf(label) + label.length).match(/^<input([^>]*)>/)
      assert.ok(input, `Missing ${label} input`)
      assert.match(input[1], /type="text"/, `${label} must use the shared input theme`)
    }
    const folder = body.match(/Album folder \(optional\)<\/legend>(.*?)<\/fieldset>/s)?.[1]
    assert.ok(folder)
    assert.match(folder, />Browse<\/button>/)
    assert.match(folder, /no local spectrals/)
  } finally {
    await server.close()
  }
})

test('report tracker detection accepts HTTP, whitespace and host casing', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    const { reportTrackerFromTarget } = await server.ssrLoadModule('/src/lib/tools.ts')
    for (const target of [
      'https://orpheus.network/torrents.php?torrentid=123',
      ' http://orpheus.network/torrents.php?torrentid=123 ',
      'https://Orpheus.Network/torrents.php?torrentid=123',
    ]) assert.equal(reportTrackerFromTarget(target), 'OPS')
    assert.equal(reportTrackerFromTarget('https://redacted.sh/torrents.php?torrentid=123'), 'RED')
    assert.equal(reportTrackerFromTarget('123'), '')
    assert.equal(reportTrackerFromTarget('https://orpheus.network.evil.test/torrents.php?torrentid=123'), '')
  } finally {
    await server.close()
  }
})
