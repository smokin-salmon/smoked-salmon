/**
 * Whether what this page sends crosses the network unencrypted: loaded over plain http from another machine.
 *
 * salmon web serves plain HTTP (ADR 0004); behind a TLS reverse proxy the browser is on https, and through an SSH
 * tunnel it is on localhost. Only the browser knows which: by the time the server sees a request, it has been sent.
 */
export function sentInClear(where: { protocol: string; hostname: string } = location): boolean {
  if (where.protocol === 'https:') return false
  const host = where.hostname.replace(/^\[|\]$/g, '').toLowerCase()
  const loopback = host === 'localhost' || host.endsWith('.localhost') || host === '::1' || inLoopbackRange(host)
  return !loopback
}

/** Whether `host` is a dotted IPv4 address in 127.0.0.0/8: not a name such as 127.example.com. */
function inLoopbackRange(host: string): boolean {
  const parts = host.split('.')
  return parts.length === 4 && parts[0] === '127' && parts.every((p) => /^\d{1,3}$/.test(p) && Number(p) <= 255)
}
