<script lang="ts">
  import { apiGet } from './api'

  let { value = $bindable('') }: { value: string } = $props()

  interface Folder {
    name: string
    path: string
    audio: boolean
    library?: boolean
  }

  interface Listing {
    path: string | null
    parent: string | null
    library: boolean
    audio: boolean
    folders: Folder[]
    truncated: boolean
    roots: { path: string; name: string; library: boolean }[]
  }

  let open = $state(false)
  let listing = $state<Listing | null>(null)
  let error = $state('')
  let loading = $state(false)

  /** The roots (no path), or the folders in one; the server refuses anything outside the roots. */
  async function browse(path?: string | null) {
    loading = true
    error = ''
    try {
      listing = await apiGet<Listing>(path ? `/browse?path=${encodeURIComponent(path)}` : '/browse')
      open = true
    } catch (e) {
      error = String(e)
    } finally {
      loading = false
    }
  }

  function select(path: string) {
    value = path
    open = false
  }

  function within(root: string, path: string): boolean {
    return path === root || path.startsWith(root.endsWith('/') ? root : root + '/')
  }
</script>

<!-- Ported from the fork's FolderPicker (chodeus a18e0e25, styx-techno 6e91041a): roots side by side, and a
     select button on each row, apart from the name that opens it. Only folders holding audio can be picked. -->
<div class="picker">
  <div class="row">
    <input type="text" class="mono grow" bind:value placeholder="/path/to/album" />
    <button class="btn secondary" disabled={loading} onclick={() => browse(value || null)}>Browse</button>
  </div>

  {#if open && listing}
    <div class="listing">
      {#if listing.roots.length > 1}
        <div class="roots">
          {#each listing.roots as root (root.path)}
            <button
              class="root"
              class:current={listing.path !== null && within(root.path, listing.path)}
              onclick={() => browse(root.path)}
            >
              {root.name}{root.library ? ' (library)' : ''}
            </button>
          {/each}
        </div>
      {/if}
      <div class="row here">
        <span class="mono grow">{listing.path ?? 'Your folders'}</span>
        {#if listing.path && listing.audio}
          <button class="btn small" onclick={() => select(listing!.path!)}>Select this folder</button>
        {/if}
        <button class="btn small secondary" onclick={() => (open = false)}>Close</button>
      </div>
      <ul>
        {#if listing.parent}
          <li><button class="nav" onclick={() => browse(listing!.parent)}>..</button></li>
        {:else if listing.path}
          <li><button class="nav" onclick={() => browse(null)}>..</button></li>
        {/if}
        {#each listing.folders as folder (folder.path)}
          <li>
            <button class="nav" onclick={() => browse(folder.path)}>
              {folder.name}/{#if folder.library}<span class="muted"> (library)</span>{/if}
            </button>
            {#if folder.audio}
              <span class="chip ok" title="Holds audio files">audio</span>
              <button class="btn small secondary" onclick={() => select(folder.path)}>Select</button>
            {/if}
          </li>
        {:else}
          <li class="muted">No folders here.</li>
        {/each}
      </ul>
      {#if listing.truncated}
        <p class="muted small">Only the first {listing.folders.length} folders are listed: type the path to reach another.</p>
      {/if}
    </div>
  {/if}
  {#if error}<p class="error">{error}</p>{/if}
</div>

<style>
  .listing {
    margin-top: 0.5rem;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 0.7rem;
    max-height: 340px;
    overflow-y: auto;
    background: var(--bg);
  }
  .roots {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-bottom: 0.5rem;
  }
  .root {
    font-size: 0.78rem;
    padding: 0.25rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--bg-raised);
    color: var(--text-dim);
    cursor: pointer;
  }
  .root.current {
    border-color: var(--accent);
    color: var(--text);
  }
  .here {
    flex-wrap: wrap;
    margin-bottom: 0.3rem;
  }
  .here .mono {
    overflow-wrap: anywhere;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  li button.nav {
    background: none;
    border: none;
    color: var(--text);
    cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    padding: 0.15rem 0.3rem;
    flex: 1;
    text-align: left;
    border-radius: 5px;
    overflow-wrap: anywhere;
  }
  li button.nav:hover {
    background: var(--bg-hover);
    color: var(--accent);
  }
  .small {
    font-size: 0.8rem;
    margin: 0.4rem 0 0;
  }
  .error {
    color: var(--err);
    margin: 0.4rem 0 0;
    font-size: 0.85rem;
  }
</style>
