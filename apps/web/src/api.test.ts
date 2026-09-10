import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { HttpSmartFillClient, type BrowserJob } from './api'

const task = {
  id: 'task-1',
  name: '联调',
  target_origin: 'http://127.0.0.1:8000',
  record_count: 1,
  status: 'draft',
  created_at: '2026-08-31T00:00:00Z',
  updated_at: '2026-08-31T00:00:00Z',
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

class MockWebSocket {
  static latest: MockWebSocket
  listeners = new Map<string, Array<(event: Event | MessageEvent) => void>>()
  sent: string[] = []
  closed = false

  constructor(readonly url: string) {
    MockWebSocket.latest = this
  }

  addEventListener(type: string, listener: (event: Event | MessageEvent) => void) {
    const listeners = this.listeners.get(type) ?? []
    listeners.push(listener)
    this.listeners.set(type, listeners)
  }

  emit(type: string, event: Event | MessageEvent = new Event(type)) {
    this.listeners.get(type)?.forEach((listener) => listener(event))
  }

  send(value: string) {
    this.sent.push(value)
  }

  close() {
    this.closed = true
  }
}

describe('HttpSmartFillClient', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:screenshot') })
  })

  afterEach(() => vi.unstubAllGlobals())

  it('calls every task and browser endpoint with in-memory bearer authentication', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(task)))
    const client = new HttpSmartFillClient(() => ' local-token ')

    await client.createTask({ name: '联调', target_origin: task.target_origin, record_count: 1 })
    await client.validateTask(task.id)
    await client.startTask(task.id)
    await client.createBrowserJob({
      task_id: task.id,
      target_url: `${task.target_origin}/demo/target`,
      fields: { 'person.fullName': '张三' },
    })
    await client.resolveBrowserJob('job-1', {})
    await client.cancelBrowserJob('job-1')
    await client.closeBrowserJob('job-1')
    await client.listJobs()
    await client.getJob('job-1')
    await client.getTargetOrigins()
    await client.replaceTargetOrigins(['https://target.example.com'])

    expect(fetchMock).toHaveBeenCalledTimes(11)
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: 'POST',
      headers: { Authorization: 'Bearer local-token', 'Content-Type': 'application/json' },
    })
    expect(fetchMock.mock.calls[4][0]).toBe('/api/v1/browser/jobs/job-1/resolve')
    expect(fetchMock.mock.calls[5][0]).toBe('/api/v1/browser/jobs/job-1/cancel')
    expect(fetchMock.mock.calls[6][0]).toBe('/api/v1/browser/jobs/job-1/browser/close')
    expect(fetchMock.mock.calls[7][0]).toBe('/api/v1/browser/jobs')
    expect(fetchMock.mock.calls[8][0]).toBe('/api/v1/browser/jobs/job-1')
    expect(fetchMock.mock.calls[9][0]).toBe('/api/v1/settings/target-origins')
    expect(fetchMock.mock.calls[10][1]).toMatchObject({
      method: 'PUT',
      body: JSON.stringify({ origins: ['https://target.example.com'] }),
    })
  })

  it('authenticates the same-origin websocket and forwards state updates', () => {
    const client = new HttpSmartFillClient(() => 'ws-token')
    const onUpdate = vi.fn()
    const disconnect = client.connectJobStream('job-1', onUpdate)
    const socket = MockWebSocket.latest
    const job = { id: 'job-1', status: 'filling' } as BrowserJob

    socket.emit('open')
    socket.emit('message', new MessageEvent('message', { data: JSON.stringify(job) }))

    expect(socket.url).toContain('/api/v1/browser/jobs/job-1/stream')
    expect(socket.sent).toEqual([JSON.stringify({ token: 'ws-token' })])
    expect(onUpdate).toHaveBeenCalledWith(job)
    disconnect()
    expect(socket.closed).toBe(true)
  })

  it('uploads imports and batches as authenticated multipart requests', async () => {
    fetchMock.mockClear()
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 'batch-1' })))
    const client = new HttpSmartFillClient(() => 'batch-token')
    const file = new File(['name\nAlice\n'], 'people.csv', { type: 'text/csv' })

    await client.previewImport(file, {})
    await client.createBatch({
      name: '九月导入',
      workflowJobId: 'job-1',
      file,
      mapping: { name: 'person.fullName' },
    })
    await client.listBatches()
    await client.getBatch('batch-1')

    const previewRequest = fetchMock.mock.calls[0][1] as RequestInit
    const batchRequest = fetchMock.mock.calls[1][1] as RequestInit
    expect(previewRequest.body).toBeInstanceOf(FormData)
    expect(previewRequest.headers).toEqual({ Authorization: 'Bearer batch-token' })
    expect(batchRequest.body).toBeInstanceOf(FormData)
    expect((batchRequest.body as FormData).get('workflow_job_id')).toBe('job-1')
    expect(batchRequest.headers).not.toHaveProperty('Content-Type')
    expect(fetchMock.mock.calls[2][0]).toBe('/api/v1/batches')
    expect(fetchMock.mock.calls[3][0]).toBe('/api/v1/batches/batch-1')
  })

  it('returns screenshot object URLs, handles missing images, and surfaces API errors', async () => {
    const client = new HttpSmartFillClient(() => '')
    fetchMock
      .mockResolvedValueOnce(new Response(new Blob(['png']), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(jsonResponse({ detail: '任务状态错误' }, 409))
      .mockResolvedValueOnce(new Response('gateway error', { status: 502 }))

    await expect(client.fetchScreenshot('job-1')).resolves.toBe('blob:screenshot')
    await expect(client.fetchScreenshot('job-2')).resolves.toBeNull()
    await expect(client.listJobs()).rejects.toThrow('任务状态错误')
    await expect(client.listJobs()).rejects.toThrow('请求失败（502）')
  })
})
