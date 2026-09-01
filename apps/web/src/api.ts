export type TaskStatus =
  | 'draft'
  | 'validating'
  | 'ready'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type BrowserJobStatus =
  | 'queued'
  | 'starting'
  | 'navigating'
  | 'entering'
  | 'observing'
  | 'filling'
  | 'verifying'
  | 'submitting'
  | 'need_human'
  | 'resuming'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface Task {
  id: string
  name: string
  target_origin: string
  record_count: number
  status: TaskStatus
  created_at: string
  updated_at: string
}

export interface JobEvent {
  sequence: number
  status: BrowserJobStatus
  message: string
  field: string | null
  created_at: string
  step_index?: number | null
  step_name?: string | null
}

export interface InterventionCandidate {
  element_id: string
  accessible_name: string
  role: string
  tag: string
  frame_path: string
  confidence: number
}

export interface FieldCandidateSet {
  canonical_field: string
  candidates: InterventionCandidate[]
}

export interface HumanIntervention {
  kind: 'field_mapping' | 'human_challenge' | 'verification' | 'submission_confirmation' | 'entry_action_confirmation'
  instruction: string
  field_candidates: FieldCandidateSet[]
  submission_candidates: InterventionCandidate[]
  entry_candidates: InterventionCandidate[]
  requires_browser_interaction: boolean
}

export interface HumanResolution {
  field_mappings: Record<string, string>
  approve_submission?: boolean
  submit_element_id?: string | null
  approve_entry_action?: boolean
  entry_element_id?: string | null
}

export type EntryActionMode = 'direct' | 'click'

export interface EntryActionConfig {
  mode: EntryActionMode
  aliases: string[]
}

export type SubmissionPolicy = 'fill_only' | 'confirm_before_submit' | 'auto_submit'

export interface SubmissionConfig {
  policy: SubmissionPolicy
  button_aliases: string[]
}

export type FieldInputKind = 'text' | 'password' | 'email' | 'tel' | 'select'

export interface FieldDefinition {
  key: string
  display_name: string
  aliases: string[]
  input_kind: FieldInputKind
  sensitive: boolean
  source_field: string | null
  autocomplete_hints: string[]
}

export interface PageScanRequest {
  target_url: string
  entry_action: EntryActionConfig
}

export interface PageScanResult {
  initial_url: string
  final_url: string
  entry_action_performed: boolean
  fields: FieldDefinition[]
}

export interface BrowserJob {
  id: string
  task_id: string
  name?: string
  target_url: string
  field_names: string[]
  status: BrowserJobStatus
  message: string
  current_url: string | null
  current_field: string | null
  completed_fields: number
  total_fields: number
  submission_policy: SubmissionPolicy
  submitted: boolean
  entry_action_mode: EntryActionMode
  entry_action_performed: boolean
  current_step?: number
  current_step_name?: string
  total_steps?: number
  configuration_snapshot?: {
    version?: number
    steps?: Array<{
      id: string
      name: string
      target_url: string
      field_names: string[]
      field_definitions: FieldDefinition[]
      entry_action: EntryActionConfig
      submission: SubmissionConfig
    }>
  }
  screenshot_url: string | null
  intervention?: HumanIntervention | null
  events: JobEvent[]
  created_at: string
  updated_at: string
}

export interface BrowserJobPayload {
  task_id: string
  target_url: string
  fields: Record<string, string>
  field_definitions?: FieldDefinition[]
  submission?: SubmissionConfig
  entry_action?: EntryActionConfig
  workflow_steps?: WorkflowStepPayload[]
}

export interface WorkflowStepPayload {
  id?: string
  name: string
  target_url: string
  fields: Record<string, string>
  field_definitions?: FieldDefinition[]
  submission?: SubmissionConfig
  entry_action?: EntryActionConfig
}

export interface TargetOriginSettings {
  origins: string[]
  updated_at: string
}

export interface ImportPreview {
  filename: string
  total_rows: number
  headers: string[]
  rows: Array<Record<string, string>>
  records: Array<{
    id: string
    values: Record<string, string>
    secret_refs: Record<string, string>
  }>
  warnings: string[]
}

export type BatchStatus =
  | 'queued'
  | 'running'
  | 'needs_attention'
  | 'completed'
  | 'completed_with_errors'
  | 'failed'

export interface BatchItem {
  id: string
  row_number: number
  status: 'queued' | 'running' | 'completed' | 'failed' | 'needs_attention'
  browser_job_id: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface BatchRun {
  id: string
  task_id: string
  workflow_job_id: string
  workflow_name: string
  name: string
  source_filename: string
  status: BatchStatus
  total_records: number
  completed_records: number
  failed_records: number
  mapping_snapshot: Record<string, string>
  items: BatchItem[]
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
}

export interface CreateBatchPayload {
  name: string
  workflowJobId: string
  file: File
  mapping: Record<string, string>
}

export interface SmartFillClient {
  scanPage(payload: PageScanRequest): Promise<PageScanResult>
  createTask(payload: {
    name: string
    target_origin: string
    record_count: number
  }): Promise<Task>
  validateTask(taskId: string): Promise<Task>
  startTask(taskId: string): Promise<Task>
  createBrowserJob(payload: BrowserJobPayload): Promise<BrowserJob>
  resolveBrowserJob(jobId: string, resolution: HumanResolution): Promise<BrowserJob>
  cancelBrowserJob(jobId: string): Promise<BrowserJob>
  connectJobStream(jobId: string, onUpdate: (job: BrowserJob) => void): () => void
  fetchScreenshot(jobId: string): Promise<string | null>
  listJobs(): Promise<BrowserJob[]>
  getJob(jobId: string): Promise<BrowserJob>
  getTargetOrigins(): Promise<TargetOriginSettings>
  replaceTargetOrigins(origins: string[]): Promise<TargetOriginSettings>
  previewImport(file: File, mapping: Record<string, string>): Promise<ImportPreview>
  createBatch(payload: CreateBatchPayload): Promise<BatchRun>
  listBatches(): Promise<BatchRun[]>
  getBatch(batchId: string): Promise<BatchRun>
}

export class HttpSmartFillClient implements SmartFillClient {
  constructor(private readonly getToken: () => string) {}

  scanPage(payload: PageScanRequest): Promise<PageScanResult> {
    return this.request('/api/v1/browser/page-scan', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  }

  createTask(payload: {
    name: string
    target_origin: string
    record_count: number
  }): Promise<Task> {
    return this.request('/api/v1/tasks', { method: 'POST', body: JSON.stringify(payload) })
  }

  validateTask(taskId: string): Promise<Task> {
    return this.request(`/api/v1/tasks/${taskId}/validate`, { method: 'POST' })
  }

  startTask(taskId: string): Promise<Task> {
    return this.request(`/api/v1/tasks/${taskId}/start`, { method: 'POST' })
  }

  createBrowserJob(payload: BrowserJobPayload): Promise<BrowserJob> {
    return this.request('/api/v1/browser/jobs', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  }

  resolveBrowserJob(jobId: string, resolution: HumanResolution): Promise<BrowserJob> {
    return this.request(`/api/v1/browser/jobs/${jobId}/resolve`, {
      method: 'POST',
      body: JSON.stringify(resolution),
    })
  }

  cancelBrowserJob(jobId: string): Promise<BrowserJob> {
    return this.request(`/api/v1/browser/jobs/${jobId}/cancel`, { method: 'POST' })
  }

  listJobs(): Promise<BrowserJob[]> {
    return this.request('/api/v1/browser/jobs')
  }

  getJob(jobId: string): Promise<BrowserJob> {
    return this.request(`/api/v1/browser/jobs/${jobId}`)
  }

  getTargetOrigins(): Promise<TargetOriginSettings> {
    return this.request('/api/v1/settings/target-origins')
  }

  replaceTargetOrigins(origins: string[]): Promise<TargetOriginSettings> {
    return this.request('/api/v1/settings/target-origins', {
      method: 'PUT',
      body: JSON.stringify({ origins }),
    })
  }

  previewImport(file: File, mapping: Record<string, string>): Promise<ImportPreview> {
    const body = new FormData()
    body.set('file', file)
    body.set('mapping_json', JSON.stringify(mapping))
    return this.request('/api/v1/imports/preview', { method: 'POST', body })
  }

  createBatch(payload: CreateBatchPayload): Promise<BatchRun> {
    const body = new FormData()
    body.set('name', payload.name)
    body.set('workflow_job_id', payload.workflowJobId)
    body.set('mapping_json', JSON.stringify(payload.mapping))
    body.set('file', payload.file)
    return this.request('/api/v1/batches', { method: 'POST', body })
  }

  listBatches(): Promise<BatchRun[]> {
    return this.request('/api/v1/batches')
  }

  getBatch(batchId: string): Promise<BatchRun> {
    return this.request(`/api/v1/batches/${batchId}`)
  }

  connectJobStream(jobId: string, onUpdate: (job: BrowserJob) => void): () => void {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(
      `${protocol}//${window.location.host}/api/v1/browser/jobs/${jobId}/stream`,
    )
    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({ token: this.getToken() }))
    })
    socket.addEventListener('message', (event) => {
      onUpdate(JSON.parse(String(event.data)) as BrowserJob)
    })
    return () => socket.close()
  }

  async fetchScreenshot(jobId: string): Promise<string | null> {
    const response = await fetch(`/api/v1/browser/jobs/${jobId}/screenshot`, {
      headers: this.headers(false),
      cache: 'no-store',
    })
    if (response.status === 404) return null
    if (!response.ok) throw await this.toError(response)
    return URL.createObjectURL(await response.blob())
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(path, {
      ...init,
      headers: {
        ...this.headers(init.body !== undefined && !(init.body instanceof FormData)),
        ...init.headers,
      },
    })
    if (!response.ok) throw await this.toError(response)
    return (await response.json()) as T
  }

  private headers(json: boolean): HeadersInit {
    const headers: Record<string, string> = {}
    const token = this.getToken().trim()
    if (token) headers.Authorization = `Bearer ${token}`
    if (json) headers['Content-Type'] = 'application/json'
    return headers
  }

  private async toError(response: Response): Promise<Error> {
    try {
      const body = (await response.json()) as { detail?: string }
      return new Error(body.detail || `请求失败（${response.status}）`)
    } catch {
      return new Error(`请求失败（${response.status}）`)
    }
  }
}
