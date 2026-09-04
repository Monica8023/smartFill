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
    screenshot_url: null,
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
    scanPage: vi.fn().mockResolvedValue({
      initial_url: 'http://127.0.0.1:8000/demo/home',
      final_url: 'http://127.0.0.1:8000/demo/login',
      entry_action_performed: true,
      fields: [
        {
          key: 'account.username',
          display_name: '用户名',
          aliases: ['账号', 'username'],
          input_kind: 'text',
          sensitive: false,
          source_field: null,
          autocomplete_hints: ['username'],
        },
        {
          key: 'account.password',
          display_name: '密码',
          aliases: ['密码', 'password'],
          input_kind: 'password',
          sensitive: true,
          source_field: null,
          autocomplete_hints: ['current-password'],
        },
      ],
    }),
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
    expect(screen.getByLabelText('进入表单方式')).toHaveValue('click')
    expect(screen.getByLabelText('入口按钮别名')).toHaveValue('Add Note')

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

    expect(await screen.findByText('填写完成')).toBeVisible()
    expect(client.createTask).toHaveBeenCalledOnce()
    expect(client.validateTask).toHaveBeenCalledWith('task-1')
    expect(client.startTask).toHaveBeenCalledWith('task-1')
    expect(client.createBrowserJob).toHaveBeenCalledWith(
      expect.objectContaining({
        task_id: 'task-1',
        fields: expect.objectContaining({ 'person.fullName': '张三' }),
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

  it('adds and submits a custom semantic field definition without code changes', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '添加自定义字段' }))
    const customGroup = screen.getByRole('group', { name: '自定义字段 1 字段配置' })
    await user.clear(within(customGroup).getByLabelText('字段标识'))
    await user.type(within(customGroup).getByLabelText('字段标识'), 'person.firstName')
    await user.clear(within(customGroup).getByLabelText('显示名称'))
    await user.type(within(customGroup).getByLabelText('显示名称'), '名')
    await user.clear(within(customGroup).getByLabelText('页面别名'))
    await user.type(within(customGroup).getByLabelText('页面别名'), 'First Name, Given Name, 名')
    await user.type(screen.getByLabelText('名'), 'San')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      fields: expect.objectContaining({ 'person.firstName': 'San' }),
      field_definitions: expect.arrayContaining([
        expect.objectContaining({
          key: 'person.firstName',
          display_name: '名',
          aliases: ['First Name', 'Given Name', '名'],
        }),
      ]),
    }))
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

  it('lets the operator confirm an ambiguous field and resume the browser session', async () => {
    const client = createClient()
    const baseJob = await client.createBrowserJob({
      task_id: 'task-1',
      target_url: 'http://127.0.0.1:8000/demo/target',
      fields: { 'person.fullName': '张三' },
    })
    const waiting: BrowserJob = {
      ...baseJob,
      status: 'need_human',
      message: '请选择姓名控件',
      intervention: {
        kind: 'field_mapping',
        instruction: '为姓名选择正确控件',
        requires_browser_interaction: true,
        submission_candidates: [],
        entry_candidates: [],
        field_candidates: [{
          canonical_field: 'person.fullName',
          candidates: [
            {
              element_id: 'sf-human-1',
              accessible_name: '用户姓名',
              role: 'textbox',
              tag: 'input',
              frame_path: 'main/profile-frame',
              confidence: 0.92,
            },
            {
              element_id: 'sf-human-2',
              accessible_name: '',
              role: '',
              tag: 'input',
              frame_path: 'main',
              confidence: 0,
            },
          ],
        }],
      },
    }
    vi.mocked(client.connectJobStream).mockImplementation((_jobId, onUpdate) => {
      onUpdate(waiting)
      return () => undefined
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))
    expect(await screen.findByRole('heading', { name: '需要人工确认' })).toBeVisible()
    expect(screen.getAllByText(/main\/profile-frame/)[0]).toBeVisible()
    expect(screen.getByRole('note')).toHaveTextContent('不会尝试破解验证码')
    expect(screen.getAllByText(/sf-human-2/)[0]).toBeVisible()
    await user.click(screen.getByRole('button', { name: '确认并重新扫描' }))

    expect(client.resolveBrowserJob).toHaveBeenCalledWith('job-1', {
      field_mappings: { 'person.fullName': 'sf-human-1' },
    })

    await user.click(await screen.findByRole('button', { name: '终止任务' }))
    expect(client.cancelBrowserJob).toHaveBeenCalledWith('job-1')
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

  it('sends the selected auto-submit policy and semantic button aliases', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.type(screen.getByLabelText('姓名'), '张三')
    await user.selectOptions(screen.getByLabelText('提交策略'), 'auto_submit')
    await user.clear(screen.getByLabelText('提交按钮别名'))
    await user.type(screen.getByLabelText('提交按钮别名'), 'Register, 注册')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      submission: {
        policy: 'auto_submit',
        button_aliases: ['Register', '注册'],
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
        field_candidates: [],
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
      field_mappings: {},
      approve_submission: true,
      submit_element_id: 'sf-submit-job-0',
    })
  })

  it('clicks a configured login entry during scan and loads discovered fields', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.selectOptions(screen.getByLabelText('进入表单方式'), 'click')
    await user.clear(screen.getByLabelText('入口按钮别名'))
    await user.type(screen.getByLabelText('入口按钮别名'), '登录, Login')
    await user.click(screen.getByRole('button', { name: '扫描页面字段' }))

    expect(client.scanPage).toHaveBeenCalledWith({
      target_url: expect.stringMatching(/^http:\/\/(localhost|127\.0\.0\.1):8000\/demo\/target$/),
      entry_action: { mode: 'click', aliases: ['登录', 'Login'] },
    })
    expect(await screen.findByText(/已从.*demo\/login.*发现 2 个字段/)).toBeVisible()
    expect(screen.getByLabelText('密码')).toHaveAttribute('type', 'password')
  })

  it('replays the login entry action before filling the discovered fields', async () => {
    const client = createClient()
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.selectOptions(screen.getByLabelText('进入表单方式'), 'click')
    await user.type(screen.getByLabelText('用户名'), 'demo-user')
    await user.type(screen.getByLabelText('登录密码'), 'temporary-password')
    await user.click(screen.getByRole('button', { name: /启动浏览器填写/ }))

    expect(client.createBrowserJob).toHaveBeenCalledWith(expect.objectContaining({
      entry_action: expect.objectContaining({
        mode: 'click',
        aliases: expect.arrayContaining(['登录', 'Login']),
      }),
    }))
  })

  it('shows a safe message when page scan finds no supported fields', async () => {
    const client = createClient()
    vi.mocked(client.scanPage).mockResolvedValue({
      initial_url: 'http://127.0.0.1:8000/',
      final_url: 'http://127.0.0.1:8000/login',
      entry_action_performed: true,
      fields: [],
    })
    const user = userEvent.setup()
    render(<App client={client} />)

    await user.click(screen.getByRole('button', { name: '扫描页面字段' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('没有发现可填写字段')
    expect(screen.getByRole('button', { name: '扫描页面字段' })).toBeEnabled()
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
        field_candidates: [],
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
      field_mappings: {},
      approve_entry_action: true,
      entry_element_id: 'sf-entry-job-0',
    })
  })
})
