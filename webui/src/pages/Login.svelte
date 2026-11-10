<script lang="ts">
  import { login } from '../lib/api'

  let { onLoggedIn, initialError = '' }: { onLoggedIn: () => void; initialError?: string } = $props()

  let token = $state('')
  // svelte-ignore state_referenced_locally
  let error = $state(initialError)
  let busy = $state(false)

  async function submit(event: Event) {
    event.preventDefault()
    if (!token || busy) return
    busy = true
    error = ''
    try {
      const ok = await login(token)
      if (ok) {
        onLoggedIn()
      } else {
        error = "访问令牌不正确。"
        token = ''
      }
    } catch (e) {
      error = `登录失败： ${e}`
    } finally {
      busy = false
    }
  }
</script>

<div class="login">
  <form onsubmit={submit}>
    <div class="brand"><span class="fish">🐟</span> salmon<span class="accent">web</span></div>
    <p>请输入访问令牌，或打开程序启动窗口中显示的登录链接。</p>
    <!-- svelte-ignore a11y_autofocus -->
    <input type="password" bind:value={token} placeholder="访问令牌" autofocus autocomplete="current-password" />
    <button type="submit" disabled={busy || !token}>{busy ? "检查中…" : "登录"}</button>
    {#if error}<div class="err">{error}</div>{/if}
  </form>
</div>

<style>
  .login {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
  }
  form {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 2rem;
    width: 100%;
    max-width: 320px;
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
  }
  .brand {
    font-weight: 700;
    font-size: 1.4rem;
  }
  .brand .accent {
    color: var(--accent);
  }
  p {
    color: var(--text-dim);
    margin: 0;
  }
  input {
    padding: 0.6rem 0.7rem;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
  }
  button {
    padding: 0.6rem;
    border-radius: 8px;
    border: none;
    background: var(--accent);
    color: white;
    font-weight: 600;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .err {
    color: var(--err, #e5484d);
    font-size: 0.9rem;
  }
</style>
