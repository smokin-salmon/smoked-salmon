/**
 * Whether what this page sends crosses the network unencrypted: loaded over plain http from another machine.
 *
 * salmon web serves plain HTTP (ADR 0004); behind a TLS reverse proxy the browser is on https, and through an SSH
 * tunnel it is on localhost. Only the browser knows which: by the time the server sees a request, it has been sent.
 */
export function sentInClear(where: { protocol: string; hostname: string } = location): boolean {
  if (where.protocol === 'https:') return false
  const host = where.hostname.replace(/^\[|\]$/g, '').toLowerCase()
  const loopback = host === 'localhost' || host.endsWith('.localhost') || host === '::1' || /^127\./.test(host)
  return !loopback
}
