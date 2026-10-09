<script lang="ts">
  import logo from './assets/salmon-logo.png'
  import Login from './pages/Login.svelte'
  import { checkAuth, login, logout, onUnauthorized, takeTokenFromUrl } from './lib/api'

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
    <div class="spacer"></div>
    <button class="btn secondary small" onclick={signOut}>Log out</button>
  </nav>

  <main>
    <p class="muted">Nothing here yet.</p>
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
