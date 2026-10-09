/** A row of `salmon check all`, as the checks job returns it (checks/verdicts.py). */
export type Verdict = 'OK' | 'WARN' | 'BLOCK' | 'INFO'

export interface Row {
  verdict: Verdict
  check: string
  detail: string
  notes: string[]
}

export interface ChecksResult {
  folder: string
  // The trackers searched for a dupe; their rows are among the others.
  trackers: string[]
  rows: Row[]
  blocking: number
  warnings: number
  report: string | null
}

// Ported from the fork's verdicts.ts (chodeus, 9bfdddc3), with check all's verdicts.
export const CHIP: Record<Verdict, string> = { OK: 'ok', WARN: 'warn', BLOCK: 'err', INFO: '' }
