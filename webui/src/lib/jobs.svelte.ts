import { apiGet, wsUrl } from './api'

export type JobStatus = 'queued' | 'running' | 'waiting' | 'done' | 'failed' | 'cancelled' | 'unknown_outcome'

export const FINISHED: JobStatus[] = ['done', 'failed', 'cancelled', 'unknown_outcome']

export interface Question {
  id: string
  kind: 'prompt' | 'confirm' | 'edit' | 'spectrals'
  text: string
  default?: string | boolean | null
  choices?: string[] | null
  initial?: string
  files?: string[]
  tracks?: Record<string, string>
  error?: string | null
}

export interface LogLine {
  n: number
  text: string
  err: boolean
}

export interface Job {
  id: string
  kind: string
  title: string
  params: Record<string, unknown>
  dry_run: boolean
  assume_defaults: boolean
  status: JobStatus
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  result: unknown
  question: Question | null
  spectrals: string[] | null
  log: LogLine[]
}

// As many lines as the server keeps.
const LOG_CAP = 5000

/**
 * The jobs, kept up to date by the server's events.
 *
 * Ported from the fork's jobs.svelte.ts (styx-techno, chodeus). The list comes without logs: a job's log is
 * loaded when it is shown, and its lines are numbered, so the lines that arrive meanwhile are neither lost nor
 * doubled. Events missed while disconnected are made up for by loading everything again on reconnecting.
 */
class JobStore {
  jobs = $state<Job[]>([])
  connected = $state(false)
  loadError = $state('')
  private ws: WebSocket | null = null
  private running = false
  private retry: ReturnType<typeof setTimeout> | null = null
  private buffer: any[] | null = null
  private resyncing = false
  private resyncAgain = false
  // The jobs whose whole log is shown.
  private shown = new Set<string>()

  start() {
    if (this.running) return
    this.running = true
    void this.connect()
  }

  /** On logging out: no more reconnecting. */
  stop() {
    this.running = false
    if (this.retry) clearTimeout(this.retry)
    this.ws?.close()
    this.ws = null
    this.jobs = []
    this.shown.clear()
  }

  get(id: string): Job | undefined {
    return this.jobs.find((j) => j.id === id)
  }

  /** Load a job's whole log, now and after each reconnection. */
  async show(id: string) {
    this.shown.add(id)
    await this.loadLog(id)
  }

  hide(id: string) {
    this.shown.delete(id)
  }

  private async connect() {
    if (!this.running) return
    try {
      // A failed websocket says nothing of why: a 401 here sends the browser back to the login.
      await apiGet('/auth')
    } catch {
      this.scheduleReconnect()
      return
    }
    if (!this.running) return
    const ws = new WebSocket(wsUrl())
    this.ws = ws
    ws.onopen = () => {
      this.connected = true
      void this.resync()
    }
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null
      this.connected = false
      this.scheduleReconnect()
    }
    ws.onmessage = (msg) => {
      const event = JSON.parse(msg.data)
      if (this.buffer !== null) this.buffer.push(event)
      else this.apply(event)
    }
  }

  private scheduleReconnect() {
    if (!this.running) return
    if (this.retry) clearTimeout(this.retry)
    this.retry = setTimeout(() => void this.connect(), 2000)
  }

  /** Load the list again, then apply the events that came meanwhile. */
  private async resync() {
    if (this.resyncing) {
      this.resyncAgain = true
      return
    }
    this.resyncing = true
    this.buffer = []
    try {
      const { jobs } = await apiGet<{ jobs: Job[] }>('/jobs')
      const known = new Map(this.jobs.map((j) => [j.id, j]))
      this.jobs = jobs.map((j) => ({ ...j, log: known.get(j.id)?.log ?? [] }))
      this.loadError = ''
    } catch (e) {
      this.loadError = `Could not load the jobs: ${e}`
    } finally {
      const buffered = this.buffer ?? []
      this.buffer = null
      for (const event of buffered) this.apply(event)
      this.resyncing = false
    }
    // Lines may have been missed while disconnected: load the logs on show again.
    for (const job of this.jobs) {
      if (this.shown.has(job.id) || !FINISHED.includes(job.status)) void this.loadLog(job.id)
    }
    if (this.resyncAgain) {
      this.resyncAgain = false
      void this.resync()
    }
  }

  private async loadLog(id: string) {
    try {
      const detail = await apiGet<Job>(`/jobs/${encodeURIComponent(id)}`)
      const job = this.get(id)
      if (!job) return
      const last = detail.log.length ? detail.log[detail.log.length - 1].n : 0
      // Lines that came by the websocket while the log was loading.
      job.log = [...detail.log, ...job.log.filter((line) => line.n > last)]
    } catch {
      // The job was dropped meanwhile, or the connection is down: the next resync loads it again.
    }
  }

  private apply(event: any) {
    const job: Job | undefined = event.job_id ? this.get(event.job_id) : undefined
    switch (event.event) {
      case 'created':
        this.upsert(event.job)
        break
      case 'finished':
        this.upsert(event.job)
        break
      case 'status':
        if (job) {
          job.status = event.status
          job.started_at = event.started_at ?? job.started_at
        }
        break
      case 'question':
        if (job) {
          job.question = event.question
          job.status = event.status
        }
        break
      case 'answered':
        if (job && job.question?.id === event.question_id) {
          job.question = null
          job.status = event.status
        }
        break
      case 'spectrals':
        if (job) job.spectrals = event.files
        break
      case 'log':
        if (job) {
          const last = job.log.length ? job.log[job.log.length - 1].n : 0
          if (event.line.n > last) job.log.push(event.line)
          if (job.log.length > LOG_CAP) job.log.splice(0, job.log.length - LOG_CAP)
        }
        break
    }
  }

  private upsert(update: Job) {
    const index = this.jobs.findIndex((j) => j.id === update.id)
    if (index >= 0) {
      // The events carry no log: keep the one streamed so far.
      this.jobs[index] = { ...update, log: this.jobs[index].log }
    } else {
      this.jobs.unshift({ ...update, log: [] })
    }
  }
}

export const jobStore = new JobStore()
