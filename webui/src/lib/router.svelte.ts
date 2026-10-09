/** The pages, by the hash of the address (`#/checks`): the server serves the app at every path. */
export const PAGES = ['dashboard', 'upload', 'spectrals', 'checks', 'convert', 'jobs'] as const
export type Page = (typeof PAGES)[number]

function fromHash(): Page {
  const name = location.hash.replace(/^#\/?/, '')
  return (PAGES as readonly string[]).includes(name) ? (name as Page) : 'dashboard'
}

class Router {
  page = $state<Page>(fromHash())

  constructor() {
    window.addEventListener('hashchange', () => {
      this.page = fromHash()
    })
  }
}

export const router = new Router()
