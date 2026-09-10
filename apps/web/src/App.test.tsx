import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { App } from './App'
import type { BatchRun, BrowserJob, SmartFillClient, Task } from './api'


function createClient(): SmartFillClient {
  const task: Task = {
    id: 'task-1',
    name: '联调任务',
    target_origin: 'http://127.0.0.1:8000',
    record_count: 1,
    status: 'draft',
    created_at: '2026-08-31T00:00:00Z',
    updated_at: '2026-08-31T00:00:00Z',
  }
  const completed: BrowserJob = {
    id: 'job-1',
    task_id: task.id,
    name: '登录后完善资料',
    target_url: 'http://127.0.0.1:8000/demo/target',
    field_names: ['person.fullName'],
    status: 'completed',
    message: '填写完成',
    current_url: 'http://127.0.0.1:8000/demo/target',
    current_field: null,
    completed_fields: 1,
    total_fields: 1,
    submission_policy: 'fill_only',
    submitted: false,
    entry_action_mode: 'direct',
    entry_action_performed: false,
    browser_session_open: false,
    screenshot_url: null,
    download_urls: ['/api/v1/browser/jobs/job-1/downloads/0'],
    statistics: {
      duration_ms: 12_345,
      screenshot_count: 4,
      model_call_count: 4,
      model_latency_ms: 3_210,
      browser_action_count: 3,
      click_count: 3,
      scroll_count: 0,
      wait_count: 0,
      fill_count: 0,
    },
    statistics_url: '/api/v1/browser/jobs/job-1/statistics',
    configuration_snapshot: {
      version: 1,
      steps: [{
        id: 'step-1',
        name: '登录',
        target_url: 'http://127.0.0.1:8000/demo/login',
        field_names: ['person.fullName'],
        field_definitions: [],
        entry_action: { mode: 'direct', aliases: [] },
        submission: { policy: 'fill_only', button_aliases: [] },
      }],
    },
    events: [{ sequence: 1, status: 'completed', message: '步骤执行完成', field: null, step_index: 1, step_name: '登录', created_at: '2026-08-31T00:00:01Z' }],
    created_at: '2026-08-31T00:00:00Z',
    updated_at: '2026-08-31T00:00:01Z',
  }
  const completedBatch: BatchRun = {
    id: 'batch-1',
    task_id: task.id,
    workflow_job_id: completed.id,
    workflow_name: completed.name ?? '登录后完善资料',
    name: '九月用户导入',
    source_filename: 'people.csv',
    status: 'completed',
    total_records: 2,
    completed_records: 2,
    failed_records: 0,
    mapping_snapshot: { full_name: 'person.fullName' },
    items: [],
    created_at: '2026-08-31T00:00:00Z',
    updated_at: '2026-08-31T00:00:01Z',
    started_at: '2026-08-31T00:00:00Z',
    completed_at: '2026-08-31T00:00:01Z',
  }
  return {
    createTask: vi.fn().mockResolvedValue(task),
    validateTask: vi.fn().mockResolvedValue({ ...task, status: 'ready' }),
    startTask: vi.fn().mockResolvedValue({ ...task, status: 'running' }),
    createBrowserJob: vi.fn().mockResolvedValue({ ...completed, status: 'queued' }),
    connectJobStream: vi.fn((_jobId, onUpdate) => {
      onUpdate(completed)
      return () => undefined
    }),
    fetchScreenshot: vi.fn().mockResolvedValue(null),
    listJobs: vi.fn().mockResolvedValue([completed]),
    getJob: vi.fn().mockResolvedValue(completed),
    getTargetOrigins: vi.fn().mockResolvedValue({
      origins: ['http://127.0.0.1:8000'],
      updated_at: '2026-08-31T00:00:00Z',
    }),
    replaceTargetOrigins: vi.fn().mockResolvedValue({
      origins: ['https://new.example.com'],
      updated_at: '2026-08-31T00:00:01Z',
    }),
    resolveBrowserJob: vi.fn().mockResolvedValue({ ...completed, status: 'resuming' }),
    cancelBrowserJob: vi.fn().mockResolvedValue({ ...completed, status: 'cancelled' }),
    closeBrowserJob: vi.fn().mockResolvedValue({
      ...completed,
      browser_session_open: false,
      message: '执行完成，浏览器已由操作员关闭',
    }),
    previewImport: vi.fn().mockResolvedValue({
      filename: 'people.csv',
      total_rows: 2,
      headers: ['full_name'],
      rows: [{ full_name: 'Alice' }, { full_name: 'Bob' }],
      records: [],
      warnings: [],
    }),
    createBatch: vi.fn().mockResolvedValue(completedBatch),
    listBatches: vi.fn().mockResolvedValue([completedBatch]),
    getBatch: vi.fn().mockResolvedValue(completedBatch),
  }
}


describe('SmartFill console', () => {
  it('shows persisted task history, configuration snapshot and execution timeline', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '任务列表' }))

    expect(await screen.findByText('登录后完善资料')).toBeVisible()
    await user.click(screen.getByRole('button', { name: /查看任务 登录后完善资料/ }))
    expect(await screen.findByText('配置快照')).toBeVisible()
    expect(screen.getByText('步骤执行完成')).toBeVisible()

    await user.click(screen.getByRole('button', { name: /收起任务 登录后完善资料/ }))
    expect(screen.queryByText('配置快照')).not.toBeInTheDocument()
  })

  it('allows an executing job to be terminated from the task list', async () => {
    const client = createClient()
    const baseJob = await client.getJob('job-1')
    const activeJob: BrowserJob = {
      ...baseJob,
      status: 'filling',
      message: '正在填写字段',
    }
    vi.mocked(client.listJobs).mockResolvedValue([activeJob])
    vi.mocked(client.cancelBrowserJob).mockResolvedValue({
      ...activeJob,
      status: 'cancelled',
      message: '任务已由操作员终止, 浏览器会话已释放',
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '任务列表' }))
    await user.click(await screen.findByRole('button', { name: '终止任务 登录后完善资料' }))

    await waitFor(() => expect(client.cancelBrowserJob).toHaveBeenCalledWith('job-1'))
    expect(await screen.findByText('已取消')).toBeVisible()
    expect(screen.queryByRole('button', { name: '终止任务 登录后完善资料' }))
      .not.toBeInTheDocument()
  })

  it('reuses a task from history and backfills its multi-step console draft', async () => {
    const client = createClient()
    const reusable = await client.getJob('job-1')
    vi.mocked(client.getJob).mockResolvedValue({
      ...reusable,
      name: 'Notes 场景',
      configuration_snapshot: {
        version: 1,
        steps: [
          {
            id: 'login-step',
            name: '登录',
            target_url: 'https://practice.expandtesting.com/notes/app',
            field_names: ['account.username', 'account.password'],
            field_values: { 'account.username': 'demo@example.com' },
            field_definitions: [
              {
                key: 'account.username',
                display_name: 'Email address',
                aliases: ['Email address', 'Email'],
                input_kind: 'email',
                sensitive: false,
                source_field: null,
                autocomplete_hints: ['username'],
              },
              {
                key: 'account.password',
                display_name: 'Password',
                aliases: ['Password'],
                input_kind: 'password',
                sensitive: true,
                source_field: null,
                autocomplete_hints: ['current-password'],
              },
            ],
            entry_action: { mode: 'auto', aliases: ['Login'] },
            submission: { policy: 'auto_submit', button_aliases: ['Login'] },
          },
          {
            id: 'note-step',
            name: '添加笔记',
            target_url: 'https://practice.expandtesting.com/notes/app',
            field_names: ['custom.category', 'custom.title', 'custom.description'],
            field_values: {
              'custom.category': 'Home',
              'custom.title': 'Reusable note',
              'custom.description': 'Backfilled description',
            },
            field_definitions: [
              {
                key: 'custom.category',
                display_name: 'Category',
                aliases: ['Category'],
                input_kind: 'select',
                sensitive: false,
                source_field: null,
                autocomplete_hints: [],
              },
              {
                key: 'custom.title',
                display_name: 'Title',
                aliases: ['Title'],
                input_kind: 'text',
                sensitive: false,
                source_field: null,
                autocomplete_hints: [],
              },
              {
                key: 'custom.description',
                display_name: 'Description',
                aliases: ['Description'],
                input_kind: 'text',
                sensitive: false,
                source_field: null,
                autocomplete_hints: [],
              },
            ],
            entry_action: { mode: 'click', aliases: ['Add Note'] },
            submission: { policy: 'auto_submit', button_aliases: ['Create'] },
          },
        ],
      },
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '任务列表' }))
    await screen.findByText('登录后完善资料')
    await user.click(screen.getByRole('button', {
      name: '复用任务 登录后完善资料',
    }))

    expect(await screen.findByRole('heading', {
      name: '浏览器自动化控制台',
    })).toBeVisible()
    expect(screen.getByLabelText('任务名称')).toHaveValue('Notes 场景（复用）')
    expect(screen.getByLabelText('当前步骤名称')).toHaveValue('登录')
    expect(screen.getByLabelText('Email address')).toHaveValue('demo@example.com')
    expect(screen.getByLabelText('Password')).toHaveValue('')
    await user.type(screen.getByLabelText('Password'), 'new-password')

    await user.click(screen.getByRole('button', {
      name: '编辑工作流步骤 添加笔记',
    }))
    expect(screen.getByLabelText('当前步骤名称')).toHaveValue('添加笔记')
    expect(screen.getByLabelText('Category')).toHaveValue('Home')
    expect(screen.getByLabelText('Title')).toHaveValue('Reusable note')
    expect(screen.getByLabelText('Description')).toHaveValue('Backfilled description')
    expect(screen.getByLabelText('进入表单方式')).toHaveValue('auto')
    expect(screen.queryByLabelText('入口按钮别名')).not.toBeInTheDocument()

    await user.clear(screen.getByLabelText('Category'))
    await user.clear(screen.getByLabelText('Title'))
    await user.clear(screen.getByLabelText('Description'))
    await user.click(screen.getByRole('button', {
      name: '编辑工作流步骤 登录',
    }))
    expect(screen.getByLabelText('当前步骤名称')).toHaveValue('登录')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', {
      name: '编辑工作流步骤 添加笔记',
    }))
    expect(screen.getByLabelText('Category')).toHaveValue('')
    expect(screen.getByLabelText('Title')).toHaveValue('')
    expect(screen.getByLabelText('Description')).toHaveValue('')
    await user.type(screen.getByLabelText('Category'), 'Home')
    await user.type(screen.getByLabelText('Title'), 'Reusable note')
    await user.type(screen.getByLabelText('Description'), 'Backfilled description')

    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))
    await waitFor(() => expect(client.createBrowserJob).toHaveBeenLastCalledWith(
      expect.objectContaining({
        workflow_steps: [
          expect.objectContaining({
            name: '登录',
            fields: {
              'account.username': 'demo@example.com',
              'account.password': 'new-password',
            },
          }),
          expect.objectContaining({
            name: '添加笔记',
            fields: {
              'custom.category': 'Home',
              'custom.title': 'Reusable note',
              'custom.description': 'Backfilled description',
            },
          }),
        ],
      }),
    ))
  })

  it('explains when an older task has no reusable snapshot', async () => {
    const client = createClient()
    const detail = await client.getJob('job-1')
    vi.mocked(client.getJob).mockResolvedValue({
      ...detail,
      configuration_snapshot: undefined,
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '任务列表' }))
    await screen.findByText('登录后完善资料')
    await user.click(screen.getByRole('button', {
      name: '复用任务 登录后完善资料',
    }))

    expect(await screen.findByRole('alert')).toHaveTextContent('该任务没有可复用的配置快照')
  })

  it('loads the target URL selector from the runtime allowlist', async () => {
    render(<App client={createClient()} />)

    const selector = await screen.findByLabelText('目标页面 URL')
    expect(selector).toHaveValue('http://127.0.0.1:8000')
    expect(within(selector).getByRole('option', {
      name: 'http://127.0.0.1:8000',
    })).toBeVisible()
  })

  it('uses automatic form entry planning by default', async () => {
    render(<App client={createClient()} />)

    expect(await screen.findByLabelText('进入表单方式')).toHaveValue('auto')
    expect(screen.queryByLabelText('入口按钮别名')).not.toBeInTheDocument()
  })

  it('imports user data and starts a selected workflow as a batch', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '用户导入' }))
    expect(await screen.findByRole('heading', { name: '用户数据批量执行' })).toBeVisible()
    await user.selectOptions(screen.getByLabelText('任务工作流'), 'job-1')
    const file = new File(['full_name\nAlice\nBob\n'], 'people.csv', {
      type: 'text/csv',
    })
    await user.upload(screen.getByLabelText('用户数据文件'), file)
    expect(await screen.findByText(/已读取.*共 2 条用户记录/)).toBeVisible()
    await user.selectOptions(screen.getByLabelText('姓名 数据列'), 'full_name')
    await user.click(screen.getByRole('button', { name: /开始批量执行/ }))

    await waitFor(() => expect(client.createBatch).toHaveBeenCalledWith(expect.objectContaining({
        workflowJobId: 'job-1',
        file,
        mapping: { full_name: 'person.fullName' },
      })))
    expect(await screen.findByText('九月用户导入')).toBeVisible()
  })

  it('updates the target origin allowlist from the web console', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '系统设置' }))
    const input = await screen.findByLabelText('目标网页白名单')
    await user.clear(input)
    await user.type(input, 'https://new.example.com')
    await user.click(screen.getByRole('button', { name: '保存并立即生效' }))

    expect(client.replaceTargetOrigins).toHaveBeenCalledWith(['https://new.example.com'])
  })
  it('creates a task and shows real-time browser completion', async () => {
    const client = createClient()
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      void client.getJob('job-1').then((completed) => onUpdate({
        ...completed,
        browser_session_open: true,
        message: '填写完成，浏览器保持打开',
      }))
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    expect(screen.getByRole('heading', { name: '浏览器自动化控制台' })).toBeVisible()
    expect(screen.getByLabelText('登录密码')).toHaveAttribute('type', 'password')

    await user.clear(screen.getByLabelText('任务名称'))
    await user.type(screen.getByLabelText('任务名称'), '联调任务')
    await user.type(screen.getByLabelText('用户名'), 'zhangsan')
    await user.type(screen.getByLabelText('登录密码'), 'temporary-password')
    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.selectOptions(screen.getByLabelText('性别'), 'female')
    await user.type(screen.getByLabelText('身份证号'), '110101199001011234')
    await user.type(screen.getByLabelText('手机号'), '13800000000')
    await user.type(screen.getByLabelText('邮箱'), 'zhangsan@example.com')
    await user.type(screen.getByLabelText('联系地址'), '北京市朝阳区')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(await screen.findByText('填写完成，浏览器保持打开')).toBeVisible()
    await user.click(screen.getByRole('button', { name: '关闭目标浏览器' }))
    expect(client.closeBrowserJob).toHaveBeenCalledWith('job-1')
    expect(screen.queryByRole('button', { name: '关闭目标浏览器' })).not.toBeInTheDocument()
    expect(client.createTask).toHaveBeenCalledOnce()
    expect(client.validateTask).toHaveBeenCalledWith('task-1')
    expect(client.startTask).toHaveBeenCalledWith('task-1')
    expect(client.createBrowserJob).toHaveBeenCalledWith(
      expect.objectContaining({
        task_id: 'task-1',
        fields: expect.objectContaining({ 'person.fullName': '张三' }),
        keep_browser_open: true,
      }),
    )
  })

  it('builds a login then profile workflow and submits ordered steps', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.clear(screen.getByLabelText('当前步骤名称'))
    await user.type(screen.getByLabelText('当前步骤名称'), '登录')
    await user.type(screen.getByLabelText('用户名'), 'demo-user')
    await user.type(screen.getByLabelText('登录密码'), 'temporary-password')
    await user.selectOptions(screen.getByLabelText('提交策略'), 'auto_submit')
    await user.click(screen.getByRole('button', { name: '保存当前步骤并添加下一步' }))

    expect(await screen.findByText(/上一工作流步骤已保存/)).toBeVisible()
    await user.clear(screen.getByLabelText('当前步骤名称'))
    await user.type(screen.getByLabelText('当前步骤名称'), '完善资料')
    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      fields: {},
      workflow_steps: [
        expect.objectContaining({
          name: '登录',
          fields: expect.objectContaining({
            'account.username': 'demo-user',
            'account.password': 'temporary-password',
          }),
          submission: expect.objectContaining({ policy: 'auto_submit' }),
        }),
        expect.objectContaining({
          name: '完善资料',
          fields: { 'person.fullName': '张三' },
        }),
      ],
    }))
  })

  it('keeps the API token in a password field and never writes localStorage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const user = userEvent.setup()
    render(<App client={createClient()} />)

    await user.type(screen.getByLabelText('API Token'), 'temporary-token')

    expect(screen.getByLabelText('API Token')).toHaveAttribute('type', 'password')
    expect(setItem).not.toHaveBeenCalled()
    setItem.mockRestore()
  })

  it('adds a business field intent without configuring page aliases', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '添加自定义字段' }))
    const customGroup = screen.getByRole('group', { name: '自定义字段 1 字段配置' })
    await user.clear(within(customGroup).getByLabelText('字段标识'))
    await user.type(within(customGroup).getByLabelText('字段标识'), 'person.firstName')
    await user.clear(within(customGroup).getByLabelText('显示名称'))
    await user.type(within(customGroup).getByLabelText('显示名称'), '名')
    await user.type(screen.getByLabelText('名'), 'San')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      fields: expect.objectContaining({ 'person.firstName': 'San' }),
      field_definitions: expect.arrayContaining([
        expect.objectContaining({
          key: 'person.firstName',
          display_name: '名',
          aliases: ['名', 'person.firstName'],
        }),
      ]),
    }))
  })

  it('configures business data sensitivity and source relationships only', async () => {
    const user = userEvent.setup()
    render(<App client={createClient()} />)

    await user.click(screen.getByRole('button', { name: '配置数据字段' }))
    const nameGroup = screen.getByRole('group', { name: '姓名 字段配置' })
    const kind = within(nameGroup).getByLabelText('数据类型')
    const source = within(nameGroup).getByLabelText('来源字段')
    const sensitive = within(nameGroup).getByRole('checkbox', { name: '敏感字段' })

    await user.selectOptions(kind, 'password')
    expect(sensitive).toBeChecked()
    expect(sensitive).toBeDisabled()

    await user.selectOptions(kind, 'text')
    await user.selectOptions(source, 'account.username')
    expect(sensitive).toBeDisabled()
    expect(screen.getByLabelText('姓名')).toBeDisabled()
  })

  it('shows a safe error when the control-plane call fails', async () => {
    const client = createClient()
    vi.mocked(client.createTask).mockRejectedValue(new Error('目标站点不在白名单'))
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent('目标站点不在白名单')
    expect(screen.getByRole('button', { name: /启动浏览器填写/ })).toBeEnabled()
  })

  it('deletes a newly added custom field directly beside its value input', async () => {
    const user = userEvent.setup()
    render(<App client={createClient()} />)

    await user.click(screen.getByRole('button', { name: '添加自定义字段' }))
    expect(screen.getByLabelText('自定义字段 1')).toBeVisible()

    await user.click(screen.getByRole('button', {
      name: '删除自定义字段 自定义字段 1',
    }))

    expect(screen.queryByLabelText('自定义字段 1')).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: '自定义字段 1 字段配置' }))
      .not.toBeInTheDocument()
  })

  it('sends auto-submit policy without configuring page button aliases', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.selectOptions(screen.getByLabelText('提交策略'), 'auto_submit')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      submission: {
        policy: 'auto_submit',
        button_aliases: [],
      },
    }))
  })

  it('shows a diagnostic download when the Browser Worker fails', async () => {
    const client = createClient()
    const queued = await client.createBrowserJob({
      task_id: 'task-1',
      target_url: 'http://127.0.0.1:8000/demo/target',
      fields: { 'person.fullName': '张三' },
    })
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate({
        ...queued,
        status: 'failed',
        message: 'Browser Worker 执行失败; 诊断 ID: abc123',
        diagnostic_id: 'abc123',
        diagnostic_url: '/api/v1/browser/jobs/job-1/diagnostic',
      })
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    const download = await screen.findByRole('link', { name: '下载诊断日志' })
    expect(download).toHaveAttribute(
      'href',
      '/api/v1/browser/jobs/job-1/diagnostic',
    )
  })

  it('shows final screenshot execution statistics and a downloadable log', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(await screen.findByText('12.35 秒')).toBeVisible()
    expect(screen.getByText('4 次截图')).toBeVisible()
    expect(screen.getByText('4 次模型调用')).toBeVisible()
    expect(screen.getByText('3 次浏览器动作')).toBeVisible()
    expect(screen.getByRole('link', { name: '下载统计日志' })).toHaveAttribute(
      'href',
      '/api/v1/browser/jobs/job-1/statistics',
    )
    expect(screen.getByRole('link', { name: '下载办事文档 1' })).toHaveAttribute(
      'href',
      '/api/v1/browser/jobs/job-1/downloads/0',
    )
  })

  it('allows an operator to terminate a job while it is executing', async () => {
    const client = createClient()
    const baseJob = await client.getJob('job-1')
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate({
        ...baseJob,
        status: 'observing',
        message: '正在分析页面截图',
      })
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    const stopButton = await screen.findByRole('button', { name: '终止任务' })
    expect(stopButton).toBeEnabled()
    await user.click(stopButton)

    await waitFor(() => expect(client.cancelBrowserJob).toHaveBeenCalledWith('job-1'))
    expect((await screen.findAllByText('已取消')).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: '终止任务' })).not.toBeInTheDocument()
  })

  it('requires an explicit click to approve an observed submit candidate', async () => {
    const client = createClient()
    const waiting: BrowserJob = {
      id: 'job-1',
      task_id: 'task-1',
      target_url: 'http://127.0.0.1:8000/demo/target',
      field_names: ['person.fullName'],
      status: 'need_human',
      message: '等待提交确认',
      current_url: 'http://127.0.0.1:8000/demo/target',
      current_field: null,
      completed_fields: 1,
      total_fields: 1,
      submission_policy: 'confirm_before_submit',
      submitted: false,
      entry_action_mode: 'direct',
      entry_action_performed: false,
      screenshot_url: null,
      intervention: {
        kind: 'submission_confirmation',
        instruction: '请选择提交按钮并明确确认',
        requires_browser_interaction: false,
        submission_candidates: [{
          element_id: 'sf-submit-job-0',
          accessible_name: 'Register',
          role: 'button',
          tag: 'button',
          frame_path: 'main',
          confidence: 1,
        }],
        entry_candidates: [],
      },
      events: [],
      created_at: '2026-08-31T00:00:00Z',
      updated_at: '2026-08-31T00:00:01Z',
    }
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate(waiting)
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))
    expect(await screen.findByLabelText('提交按钮候选')).toHaveValue('sf-submit-job-0')
    await user.click(screen.getByRole('button', { name: '确认并提交一次' }))

    expect(client.resolveBrowserJob).toHaveBeenCalledWith('job-1', {
      approve_submission: true,
      submit_element_id: 'sf-submit-job-0',
    })
  })

  it('does not expose legacy page scanning or DOM field mapping controls', () => {
    render(<App client={createClient()} />)

    expect(screen.queryByRole('button', { name: '扫描页面字段' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '配置字段映射' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('动态字段映射配置')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('入口按钮别名')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('提交按钮别名')).not.toBeInTheDocument()
  })

  it('uses visual AUTO entry planning without configured aliases', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('用户名'), 'demo-user')
    await user.type(screen.getByLabelText('登录密码'), 'temporary-password')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      entry_action: expect.objectContaining({
        mode: 'auto',
        aliases: [],
      }),
    }))
  })

  it('sends an identity switch, natural-language target, and five-second cadence', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.selectOptions(screen.getByLabelText('账户流程'), 'login')
    await user.clear(screen.getByLabelText('目标任务描述'))
    await user.type(
      screen.getByLabelText('目标任务描述'),
      '找到填写房产认证信息的入口并填写资料',
    )
    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      target_intent: '找到填写房产认证信息的入口并填写资料',
      authentication_mode: 'login',
      observation_interval_seconds: 5,
    }))
  })

  it('configures a reusable manual-login session and confirms browser login', async () => {
    const client = createClient()
    const baseJob = await client.createBrowserJob({
      task_id: 'task-1',
      target_url: 'http://127.0.0.1:8000/',
      fields: {},
      target_intent: '进入房产认证',
    })
    vi.mocked(client.createBrowserJob).mockClear()
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate({
        ...baseJob,
        status: 'need_human',
        message: '等待人工登录',
        intervention: {
          kind: 'manual_login',
          instruction: '请在目标浏览器完成短信验证',
          requires_browser_interaction: true,
          submission_candidates: [],
          entry_candidates: [],
        },
      })
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.selectOptions(screen.getByLabelText('账户流程'), 'manual')
    await user.type(screen.getByLabelText('登录会话标识'), 'property-account')
    await user.type(
      screen.getByLabelText('登录心跳 URL'),
      'http://127.0.0.1:8000/session/heartbeat?token=unsafe',
    )
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))
    expect(await screen.findByText('登录心跳 URL 不能包含查询参数或片段')).toBeVisible()
    expect(client.createBrowserJob).not.toHaveBeenCalled()
    await user.clear(screen.getByLabelText('登录心跳 URL'))
    await user.type(
      screen.getByLabelText('登录心跳 URL'),
      'http://127.0.0.1:8000/session/heartbeat',
    )
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      authentication_mode: 'manual',
      authentication_session_key: 'property-account',
      heartbeat_url: 'http://127.0.0.1:8000/session/heartbeat',
      heartbeat_interval_seconds: 300,
    }))
    expect(await screen.findByText('请在目标浏览器完成短信验证')).toBeVisible()
    const launchButton = screen.getByRole('button', { name: /启动浏览器填写/ })
    expect(launchButton).toBeDisabled()
    await user.click(launchButton)
    expect(client.createBrowserJob).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '我已完成登录，继续执行' }))
    expect(client.resolveBrowserJob).toHaveBeenCalledWith('job-1', {
      manual_login_completed: true,
    })
  })

  it('resumes a visual review without asking for DOM field mappings', async () => {
    const client = createClient()
    const baseJob = await client.createBrowserJob({
      task_id: 'task-1',
      target_url: 'http://127.0.0.1:8000/',
      fields: { 'person.fullName': '张三' },
    })
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate({
        ...baseJob,
        status: 'need_human',
        message: '页面需要人工处理',
        intervention: {
          kind: 'visual_review',
          instruction: '关闭遮挡后继续视觉识别',
          requires_browser_interaction: true,
          submission_candidates: [],
          entry_candidates: [],
        },
      })
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))
    expect(await screen.findByText('关闭遮挡后继续视觉识别')).toBeVisible()
    expect(screen.queryByText(/候选控件/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '处理完成，继续视觉识别' }))

    expect(client.resolveBrowserJob).toHaveBeenCalledWith('job-1', {})
  })

  it('asks for missing customer data and resumes the retained session', async () => {
    const client = createClient()
    const baseJob = await client.createBrowserJob({
      task_id: 'task-1',
      target_url: 'http://127.0.0.1:8000/',
      fields: {},
      target_intent: '填写房产认证资料',
    })
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate({
        ...baseJob,
        status: 'need_human',
        message: '缺少目标表单必填资料',
        intervention: {
          kind: 'data_required',
          instruction: '请补充以下客户资料',
          requires_browser_interaction: false,
          submission_candidates: [],
          entry_candidates: [],
          missing_fields: [
            {
              key: 'property.certificateNumber',
              display_name: '房产证号',
              input_kind: 'text',
              sensitive: true,
              reason: '目标表单必填',
            },
            {
              key: 'property.usage',
              display_name: '房屋用途',
              input_kind: 'select',
              sensitive: false,
              reason: '目标表单必填',
            },
          ],
        },
      })
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.clear(screen.getByLabelText('目标任务描述'))
    await user.type(screen.getByLabelText('目标任务描述'), '填写房产认证资料')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))
    const missing = await screen.findByLabelText('房产证号')
    expect(missing).toHaveAttribute('type', 'password')
    await user.type(missing, '沪房权证123456')
    const usage = screen.getByLabelText('房屋用途')
    expect(usage).toHaveAttribute('type', 'text')
    await user.type(usage, '住宅')
    await user.click(screen.getByRole('button', { name: '补齐资料并继续' }))

    expect(client.resolveBrowserJob).toHaveBeenCalledWith('job-1', {
      field_values: {
        'property.certificateNumber': '沪房权证123456',
        'property.usage': '住宅',
      },
    })
  })

  it('allows an operator to select an ambiguous login entry and resume', async () => {
    const client = createClient()
    const baseJob = await client.createBrowserJob({
      task_id: 'task-1',
      target_url: 'http://127.0.0.1:8000/',
      fields: { 'account.username': 'demo-user' },
    })
    const waiting: BrowserJob = {
      ...baseJob,
      status: 'need_human',
      entry_action_mode: 'click',
      message: '登录入口无法唯一识别',
      intervention: {
        kind: 'entry_action_confirmation',
        instruction: '选择登录入口',
        requires_browser_interaction: false,
        submission_candidates: [],
        entry_candidates: [{
          element_id: 'sf-entry-job-0',
          accessible_name: 'Login',
          role: 'button',
          tag: 'a',
          frame_path: 'main',
          confidence: 0.92,
        }],
      },
    }
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate(waiting)
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('用户名'), 'demo-user')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))
    expect(await screen.findByLabelText('表单入口候选')).toHaveValue('sf-entry-job-0')
    await user.click(screen.getByRole('button', { name: '确认并进入登录页' }))

    expect(client.resolveBrowserJob).toHaveBeenCalledWith('job-1', {
      approve_entry_action: true,
      entry_element_id: 'sf-entry-job-0',
    })
  })
})
