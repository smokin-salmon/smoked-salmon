import { apiGet } from './api'

/** What `/api/tools/options` says: the image hosts, and the host each tracker's images and spectrals go to. */
export interface ToolsOptions {
  /** `only_for` lists the trackers whose pages show the host's images, when it is only some. */
  image_hosts: { name: string; only_for: string[] }[]
  trackers: { code: string; image_host: string; spectrals_host: string }[]
  /** The hosts used without a tracker: [image] image_uploader and specs_uploader. */
  image_host: string
  spectrals_host: string
  max_images: number
  max_descgen_urls: number
  max_image_bytes: number
}

export function loadToolsOptions(): Promise<ToolsOptions> {
  return apiGet<ToolsOptions>('/tools/options')
}

export interface DescgenResult {
  description: string
}

export interface ImageUploadResult {
  host: string
  tracker: string | null
  images: { file: string; url: string }[]
}

export interface SpectralsUploadResult {
  host: string
  tracker: string | null
  tracks: { id: number; track: string; urls: string[] }[]
  /** What `salmon specs` prints: `name: urls`, and with `-f` as BBCode. */
  text: string
  bbcode: string
}
