<script lang="ts">
  import logo from './assets/salmon-logo.png'
  import Checks from './pages/Checks.svelte'
  import Dashboard from './pages/Dashboard.svelte'
  import Jobs from './pages/Jobs.svelte'
  import Login from './pages/Login.svelte'
  import Spectrals from './pages/Spectrals.svelte'
  import Upload from './pages/Upload.svelte'
  import { checkAuth, login, logout, onUnauthorized, takeTokenFromUrl } from './lib/api'
  import { FINISHED, jobStore } from './lib/jobs.svelte'
  import { router } from './lib/router.svelte'

  // null while the first check runs.
  let authed = $state<boolean | null>(null)
  let loginError = $state('')

  async function refreshAuth() {
    try {
      authed = await checkAuth()
    } catch {
      authed = false
    }
  }

  async function start() {
    const linkToken = takeTokenFromUrl()
    if (linkToken) {
      try {
        if (!(await login(linkToken))) loginError = 'The login link holds an old or wrong token.'
      } catch (e) {
        loginError = `Login failed: ${e}`
      }
    }
    await refreshAuth()
  }

  async function signOut() {
    try {
      await logout()
    } finally {
      authed = false
    }
  }

  onUnauthorized(() => {
    authed = false
  })

  $effect(() => {
    start()
  })

  $effect(() => {
    if (authed) jobStore.start()
    else jobStore.stop()
  })

  const active = $derived(jobStore.jobs.filter((j) => !FINISHED.includes(j.status)).length)
  const asking = $derived(jobStore.jobs.some((j) => j.question))
</script>

{#if authed === null}
  <div class="booting">Loading…</div>
{:else if !authed}
  <Login onLoggedIn={refreshAuth} initialError={loginError} />
{:else}
<div class="layout">
  <nav>
    <div class="brand">
      <img class="logo" src={logo} alt="" width="24" height="24" /> salmon<span class="accent">web</span>
    </div>
    <a href="#/dashboard" class:active={router.page === 'dashboard'}>Dashboard</a>
    <a href="#/upload" class:active={router.page === 'upload'}>Upload</a>
    <a href="#/spectrals" class:active={router.page === 'spectrals'}>Spectrals</a>
    <a href="#/checks" class:active={router.page === 'checks'}>Checks</a>
    <a href="#/jobs" class:active={router.page === 'jobs'}>
      Jobs
      {#if active > 0}<span class="chip {asking ? 'warn' : 'run'}">{active}</span>{/if}
    </a>
    <div class="spacer"></div>
    <span class="chip {jobStore.connected ? 'ok' : 'err'}">{jobStore.connected ? 'connected' : 'disconnected'}</span>
    <button class="btn secondary small" onclick={signOut}>Log out</button>
  </nav>

  <main>
    {#if router.page === 'dashboard'}
      <Dashboard />
    {:else if router.page === 'upload'}
      <Upload />
    {:else if router.page === 'spectrals'}
      <Spectrals />
    {:else if router.page === 'checks'}
      <Checks />
    {:else}
      <Jobs />
    {/if}
  </main>
</div>

{/if}

<style>
  .booting {
    padding: 2rem;
    color: var(--text-dim);
  }
  .layout {
    display: flex;
    min-height: 100vh;
  }
  nav {
    width: 200px;
    flex-shrink: 0;
    background: var(--bg-raised);
    border-right: 1px solid var(--border);
    padding: 1rem 0.8rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    font-weight: 700;
    font-size: 1.1rem;
    margin-bottom: 1rem;
    padding: 0 0.5rem;
  }
  .brand .logo {
    width: 24px;
    height: 24px;
    flex: none;
  }
  .brand .accent {
    color: var(--accent);
  }
  nav a {
    color: var(--text-dim);
    padding: 0.45rem 0.6rem;
    border-radius: 8px;
    font-weight: 500;
  }
  nav a:hover {
    background: var(--bg-hover);
    text-decoration: none;
  }
  nav a.active {
    background: var(--bg-hover);
    color: var(--text);
  }
  .spacer {
    flex: 1;
  }
  main {
    flex: 1;
    padding: 1.5rem 2rem;
    max-width: 1100px;
  }
  @media (max-width: 700px) {
    .layout {
      flex-direction: column;
    }
    nav {
      width: auto;
      flex-direction: row;
      flex-wrap: wrap;
      align-items: center;
      border-right: none;
      border-bottom: 1px solid var(--border);
    }
    .brand {
      margin-bottom: 0;
    }
    main {
      padding: 1rem;
    }
  }
</style>
