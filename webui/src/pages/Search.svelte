<script lang="ts">
  import { apiGet } from '../lib/api'

  interface Release {
    artist: string
    album: string
    year: number | string | null
    track_count: number | null
    summary: string
    url: string
  }

  interface Source {
    name: string
    status: 'found' | 'none' | 'inactive' | 'failed'
    releases: Release[]
  }

  interface Track {
    title: string
    artists?: [string, string][]
  }

  interface Metadata {
    title?: string
    artists?: [string, string][]
    year?: number | string | null
    label?: string | null
    catno?: string | null
    genres?: string[]
    tracks?: Record<string, Record<string, Track>>
    [key: string]: unknown
  }

  let query = $state('')
  // A number input: empty is null.
  let trackCount = $state<number | null>(null)
  let searching = $state(false)
  let searched = $state<{ query: string; sources: Source[] } | null>(null)
  let error = $state('')

  let lookupUrl = $state('')
  let looking = $state(false)
  let found = $state<{ url: string; source: string; metadata: Metadata } | null>(null)
  let lookupError = $state('')

  // Each search and lookup is numbered: only the latest one's answer is shown.
  let searchSeq = 0
  let lookupSeq = 0

  async function search() {
    if (!query.trim()) return
    const seq = ++searchSeq
    searching = true
    error = ''
    searched = null
    const params = new URLSearchParams({ q: query.trim() })
    if (trackCount) params.set('track_count', String(trackCount))
    try {
      const answer = await apiGet<{ query: string; sources: Source[] }>(`/search?${params}`)
      if (seq === searchSeq) searched = answer
    } catch (e) {
      if (seq === searchSeq) error = String(e)
    } finally {
      if (seq === searchSeq) searching = false
    }
  }

  async function lookUp(url: string) {
    if (!url.trim()) return
    const seq = ++lookupSeq
    lookupUrl = url.trim()
    looking = true
    lookupError = ''
    found = null
    try {
      const answer = await apiGet<{ url: string; source: string; metadata: Metadata }>(
        `/metadata?${new URLSearchParams({ url: lookupUrl })}`,
      )
      if (seq === lookupSeq) found = answer
    } catch (e) {
      if (seq === lookupSeq) lookupError = String(e)
    } finally {
      if (seq === lookupSeq) looking = false
    }
  }

  // A store's answer is shown as a link only when it is a web address.
  function isWebUrl(url: string): boolean {
    return /^https?:\/\//i.test(url)
  }

  function names(artists: [string, string][] | undefined): string {
    return (artists ?? []).map((a) => a[0]).join(', ')
  }

  function trackRows(metadata: Metadata): { at: string; title: string; artists: string }[] {
    const rows = []
    for (const [disc, tracks] of Object.entries(metadata.tracks ?? {})) {
      for (const [number, track] of Object.entries(tracks)) {
        rows.push({ at: `${disc}-${number}`, title: track.title, artists: names(track.artists) })
      }
    }
    return rows
  }

  const STATUS: Record<Source['status'], string> = {
    found: '',
    none: 'No results.',
    inactive: 'Inactive: the config has no credentials for it.',
    failed: 'The search failed.',
  }
</script>

<!-- Ported from the fork's Search page (styx-techno, 5f1f55a6; chodeus, 0b29d2d5 and 9bfdddc3). -->
<h1>Search</h1>
<p class="lead">
  What <span class="mono">salmon metas</span> and <span class="mono">salmon meta</span> do: search every metadata
  source for a release, then look up the full metadata of a result or of a release URL. Only the stores are contacted,
  never a tracker.
</p>

<div class="card">
  <form
    class="row wrap"
    onsubmit={(e) => {
      e.preventDefault()
      search()
    }}
  >
    <input type="text" class="grow" bind:value={query} placeholder="Artist and album" maxlength="200" />
    <input
      type="number"
      class="count"
      bind:value={trackCount}
      placeholder="Tracks"
      min="1"
      max="999"
      title="Keep releases with about this many tracks (-t)"
    />
    <button class="btn" disabled={searching || !query.trim()}>{searching ? 'Searching…' : 'Search'}</button>
  </form>
  <form
    class="row wrap lookup"
    onsubmit={(e) => {
      e.preventDefault()
      lookUp(lookupUrl)
    }}
  >
    <input type="text" class="grow" bind:value={lookupUrl} placeholder="A release URL" maxlength="2000" />
    <button class="btn secondary" disabled={looking || !lookupUrl.trim()}>Look up</button>
  </form>
  {#if error}<p class="error">{error}</p>{/if}
</div>

{#if looking}
  <div class="card"><p class="muted">Looking up {lookupUrl}…</p></div>
{:else if lookupError}
  <div class="card"><p class="error">{lookupError}</p></div>
{:else if found}
  {@const metadata = found.metadata}
  <div class="card">
    <h2>{found.source}</h2>
    <p>
      <strong>{names(metadata.artists)}</strong> - {metadata.title}
      <span class="muted"
        >({metadata.year ?? '?'}{metadata.label ? `, ${metadata.label}` : ''}{metadata.catno
          ? `, ${metadata.catno}`
          : ''})</span
      >
    </p>
    {#if metadata.genres?.length}
      <p class="row wrap">
        {#each metadata.genres as genre (genre)}<span class="chip">{genre}</span>{/each}
      </p>
    {/if}
    <table>
      <tbody>
        {#each trackRows(metadata) as track (track.at)}
          <tr>
            <td class="muted">{track.at}</td>
            <td>{track.title}</td>
            <td class="muted">{track.artists}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <details>
      <summary>Everything</summary>
      <pre class="mono">{JSON.stringify(metadata, null, 2)}</pre>
    </details>
  </div>
{/if}

{#if searched}
  {#each searched.sources as source (source.name)}
    <div class="card">
      <h2>{source.name}</h2>
      {#if source.status !== 'found'}
        <p class="muted">{STATUS[source.status]}</p>
      {:else}
        <table>
          <tbody>
            {#each source.releases as release (release.url)}
              <tr>
                <td class="grow">{release.summary}</td>
                <td class="links">
                  {#if isWebUrl(release.url)}
                    <a href={release.url} target="_blank" rel="noreferrer noopener">Open</a>
                  {/if}
                  <button class="btn small secondary" onclick={() => lookUp(release.url)} disabled={looking}
                    >Look up</button
                  >
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {/each}
{/if}

<style>
  .wrap {
    flex-wrap: wrap;
  }
  .count {
    width: 6rem;
  }
  .lookup {
    margin-top: 0.7rem;
  }
  .links {
    white-space: nowrap;
    text-align: right;
  }
  .links a {
    margin-right: 0.5rem;
  }
  .error {
    color: var(--err);
    margin: 0.5rem 0 0;
  }
  pre {
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 0.8rem;
  }
</style>
