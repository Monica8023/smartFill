import { useEffect, useMemo, useRef, useState } from 'react'

import {
  HttpSmartFillClient,
  type BatchRun,
  type BrowserJob,
  type BrowserJobStatus,
  type EntryActionMode,
  type FieldDefinition,
  type FieldInputKind,
  type SmartFillClient,
  type SubmissionPolicy,
  type WorkflowStepPayload,
  type ImportPreview,
} from './api'
import './styles.css'

interface AppProps {
  client?: SmartFillClient
}

interface FormState {
  taskName: string
  stepName: string
  targetUrl: string
  submissionPolicy: SubmissionPolicy
  submissionButtonAliases: string
  entryActionMode: EntryActionMode
  entryActionAliases: string
}

interface EditableField {
  id: string
  key: string
  displayName: string
  aliases: string
  inputKind: FieldInputKind
  sensitive: boolean
  sourceField: string
  autocompleteHints: string[]
  value: string
}

const statusText: Record<BrowserJobStatus, string> = {
  queued: '等待执行',
  starting: '启动浏览器',
  navigating: '访问页面',
  entering: '进入表单页',
  observing: '理解页面',
  filling: '填写字段',
  verifying: '回读验证',
  submitting: '提交表单',
  need_human: '需要人工处理',
  resuming: '重新扫描',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已取消',
}

function defaultTargetUrl(): string {
  if (window.location.port === '8000') return `${window.location.origin}/demo/target`
  return `${window.location.protocol}//${window.location.hostname}:8000/demo/target`
}

const initialForm: FormState = {
  taskName: '资料页自动填写演示',
  stepName: '填写资料',
  targetUrl: defaultTargetUrl(),
  submissionPolicy: 'fill_only',
  submissionButtonAliases: '提交, 保存, 登录, 注册, submit, save, login, register',
  entryActionMode: 'direct',
  entryActionAliases: '登录, 登陆, Login, Sign in',
}

const submissionPolicyText: Record<SubmissionPolicy, string> = {
  fill_only: '仅填写',
  confirm_before_submit: '确认后提交',
  auto_submit: '自动提交',
}

const batchStatusText: Record<BatchRun['status'], string> = {
  queued: '等待执行',
  running: '批量执行中',
  needs_attention: '需要人工处理',
  completed: '全部完成',
  completed_with_errors: '部分完成',
  failed: '执行失败',
}

function targetUrlParts(value: string): { origin: string; path: string } {
  try {
    const parsed = new URL(value)
    return { origin: parsed.origin, path: `${parsed.pathname}${parsed.search}` }
  } catch {
    return { origin: '', path: '/' }
  }
}

function fallbackFieldName(key: string): string {
  const names: Record<string, string> = {
    'account.username': '用户名',
    'account.password': '密码',
    'person.fullName': '姓名',
    'person.gender': '性别',
    'person.idNumber': '身份证号',
    'person.phone': '手机号',
    'person.email': '邮箱',
    'person.address': '地址',
  }
  return names[key] ?? key
}

const initialFields: EditableField[] = [
  { id: 'account-username', key: 'account.username', displayName: '用户名', aliases: '用户名, 账号, 登录名, username, user, login', inputKind: 'text', sensitive: false, sourceField: '', autocompleteHints: ['username'], value: '' },
  { id: 'account-password', key: 'account.password', displayName: '登录密码', aliases: '密码, password, passwd, pwd', inputKind: 'password', sensitive: true, sourceField: '', autocompleteHints: ['current-password', 'new-password'], value: '' },
  { id: 'person-full-name', key: 'person.fullName', displayName: '姓名', aliases: '姓名, 真实姓名, 名字, fullname, name', inputKind: 'text', sensitive: false, sourceField: '', autocompleteHints: ['name'], value: '' },
  { id: 'person-gender', key: 'person.gender', displayName: '性别', aliases: '性别, gender, sex', inputKind: 'select', sensitive: false, sourceField: '', autocompleteHints: [], value: '' },
  { id: 'person-id-number', key: 'person.idNumber', displayName: '身份证号', aliases: '身份证, 证件号码, 证件号, idnumber, idcard', inputKind: 'text', sensitive: true, sourceField: '', autocompleteHints: [], value: '' },
  { id: 'person-phone', key: 'person.phone', displayName: '手机号', aliases: '手机号, 手机号码, 联系电话, phone, mobile, tel', inputKind: 'tel', sensitive: true, sourceField: '', autocompleteHints: ['tel', 'tel-national'], value: '' },
  { id: 'person-email', key: 'person.email', displayName: '邮箱', aliases: '邮箱, 电子邮件, email, mail', inputKind: 'email', sensitive: false, sourceField: '', autocompleteHints: ['email'], value: '' },
  { id: 'person-address', key: 'person.address', displayName: '联系地址', aliases: '地址, 联系地址, 居住地址, address', inputKind: 'text', sensitive: false, sourceField: '', autocompleteHints: ['street-address', 'address-line1'], value: '' },
]

function parseAliases(value: string): string[] {
  return [...new Set(value.split(/[,，\n]/).map((alias) => alias.trim()).filter(Boolean))]
}

export function App({ client: injectedClient }: AppProps) {
  const [activeView, setActiveView] = useState<'console' | 'tasks' | 'import' | 'settings'>('console')
  const [apiToken, setApiToken] = useState('')
  const [form, setForm] = useState<FormState>(initialForm)
  const [fields, setFields] = useState<EditableField[]>(initialFields)
  const [showFieldConfiguration, setShowFieldConfiguration] = useState(false)
  const [job, setJob] = useState<BrowserJob | null>(null)
  const [screenshot, setScreenshot] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [scanMessage, setScanMessage] = useState('')
  const [error, setError] = useState('')
  const [humanMappings, setHumanMappings] = useState<Record<string, string>>({})
  const [humanSubmitElement, setHumanSubmitElement] = useState('')
  const [humanEntryElement, setHumanEntryElement] = useState('')
  const [taskHistory, setTaskHistory] = useState<BrowserJob[]>([])
  const [selectedHistory, setSelectedHistory] = useState<BrowserJob | null>(null)
  const [originsText, setOriginsText] = useState('')
  const [targetOrigins, setTargetOrigins] = useState<string[]>([])
  const [settingsMessage, setSettingsMessage] = useState('')
  const [savedWorkflowSteps, setSavedWorkflowSteps] = useState<WorkflowStepPayload[]>([])
  const [importWorkflows, setImportWorkflows] = useState<BrowserJob[]>([])
  const [importWorkflowId, setImportWorkflowId] = useState('')
  const [importFile, setImportFile] = useState<File | null>(null)
  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null)
  const [importMapping, setImportMapping] = useState<Record<string, string>>({})
  const [importBatchName, setImportBatchName] = useState('九月用户导入')
  const [importing, setImporting] = useState(false)
  const [batchHistory, setBatchHistory] = useState<BatchRun[]>([])
  const disconnectRef = useRef<(() => void) | null>(null)
  const screenshotRef = useRef<string | null>(null)
  const nextCustomFieldRef = useRef(1)
  const tokenRef = useRef(apiToken)
  tokenRef.current = apiToken

  const client = useMemo(
    () => injectedClient ?? new HttpSmartFillClient(() => tokenRef.current),
    [injectedClient],
  )

  useEffect(() => {
    return () => {
      disconnectRef.current?.()
      if (screenshotRef.current) URL.revokeObjectURL(screenshotRef.current)
    }
  }, [])

  const loadTargetOrigins = () => {
    client.getTargetOrigins().then((settings) => {
      setTargetOrigins(settings.origins)
      setOriginsText(settings.origins.join('\n'))
      const current = targetUrlParts(form.targetUrl)
      if (!settings.origins.includes(current.origin) && settings.origins[0]) {
        setForm((previous) => ({
          ...previous,
          targetUrl: `${settings.origins[0]}${current.path}`,
        }))
      }
    }).catch((caught: unknown) => {
      setError(caught instanceof Error ? caught.message : '目标网页白名单加载失败')
    })
  }

  useEffect(() => {
    loadTargetOrigins()
  }, [client])

  useEffect(() => {
    if (activeView === 'tasks') {
      client.listJobs().then(setTaskHistory).catch((caught: unknown) => {
        setError(caught instanceof Error ? caught.message : '任务列表加载失败')
      })
    }
    if (activeView === 'settings') {
      client.getTargetOrigins().then((settings) => {
        setOriginsText(settings.origins.join('\n'))
      }).catch((caught: unknown) => {
        setError(caught instanceof Error ? caught.message : '系统设置加载失败')
      })
    }
    if (activeView === 'import') {
      Promise.all([client.listJobs(), client.listBatches()]).then(([jobs, batches]) => {
        const workflows = jobs.filter((item) => (
          (item.configuration_snapshot?.steps?.length ?? 0) > 0
        ))
        setImportWorkflows(workflows)
        setImportWorkflowId((current) => current || workflows[0]?.id || '')
        setBatchHistory(batches)
      }).catch((caught: unknown) => {
        setError(caught instanceof Error ? caught.message : '导入工作流加载失败')
      })
    }
  }, [activeView, client])

  useEffect(() => {
    if (activeView !== 'import') return
    const refresh = window.setInterval(() => {
      client.listBatches().then(setBatchHistory).catch(() => {
        // Keep the last successful snapshot during a transient polling failure.
      })
    }, 1500)
    return () => window.clearInterval(refresh)
  }, [activeView, client])

  const openHistory = async (historyJob: BrowserJob) => {
    if (selectedHistory?.id === historyJob.id) {
      setSelectedHistory(null)
      return
    }
    try {
      setSelectedHistory(await client.getJob(historyJob.id))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '任务详情加载失败')
    }
  }

  const saveTargetOrigins = async () => {
    setError('')
    setSettingsMessage('')
    const origins = parseAliases(originsText)
    try {
      const saved = await client.replaceTargetOrigins(origins)
      setOriginsText(saved.origins.join('\n'))
      setTargetOrigins(saved.origins)
      setSettingsMessage('白名单已保存并对新请求立即生效')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '白名单保存失败')
    }
  }

  const setField = (field: keyof FormState, value: string) => {
    setForm((current) => ({ ...current, [field]: value }))
  }

  const selectedWorkflow = importWorkflows.find((item) => item.id === importWorkflowId)
  const importFields = useMemo(() => {
    if (!selectedWorkflow) return []
    const unique = new Map<string, { key: string; displayName: string }>()
    for (const step of selectedWorkflow.configuration_snapshot?.steps ?? []) {
      const definitions = new Map(step.field_definitions.map((item) => [item.key, item]))
      for (const key of step.field_names) {
        const definition = definitions.get(key)
        if (definition?.source_field) continue
        unique.set(key, {
          key,
          displayName: definition?.display_name ?? fallbackFieldName(key),
        })
      }
    }
    return [...unique.values()]
  }, [selectedWorkflow])

  const inspectImportFile = async (file: File | null) => {
    setImportFile(file)
    setImportPreview(null)
    setImportMapping({})
    setError('')
    if (!file) return
    try {
      const preview = await client.previewImport(file, {})
      setImportPreview(preview)
      setImportMapping(Object.fromEntries(importFields.map((field) => {
        const direct = preview.headers.find((header) => (
          header.toLocaleLowerCase() === field.key.toLocaleLowerCase()
          || header.toLocaleLowerCase() === field.displayName.toLocaleLowerCase()
        ))
        return [field.key, direct ?? '']
      })))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '用户数据文件解析失败')
    }
  }

  const runBatch = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!importFile || !importWorkflowId) return
    if (importFields.some((field) => !importMapping[field.key])) {
      setError('请为每个工作流字段选择一个数据列')
      return
    }
    setError('')
    setImporting(true)
    try {
      const mapping = Object.fromEntries(
        importFields.map((field) => [importMapping[field.key], field.key]),
      )
      const batch = await client.createBatch({
        name: importBatchName.trim(),
        workflowJobId: importWorkflowId,
        file: importFile,
        mapping,
      })
      setBatchHistory((current) => [batch, ...current.filter((item) => item.id !== batch.id)])
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '批量任务启动失败')
    } finally {
      setImporting(false)
    }
  }

  const updateDynamicField = (id: string, patch: Partial<EditableField>) => {
    setFields((current) => current.map((field) => (
      field.id === id ? { ...field, ...patch } : field
    )))
  }

  const addDynamicField = () => {
    const sequence = nextCustomFieldRef.current
    nextCustomFieldRef.current += 1
    setFields((current) => [
      ...current,
      {
        id: `custom-${sequence}`,
        key: `custom.field${sequence}`,
        displayName: `自定义字段 ${sequence}`,
        aliases: `自定义字段 ${sequence}`,
        inputKind: 'text',
        sensitive: false,
        sourceField: '',
        autocompleteHints: [],
        value: '',
      },
    ])
    setShowFieldConfiguration(true)
  }

  const removeDynamicField = (id: string) => {
    setFields((current) => current.filter((field) => field.id !== id))
  }

  const updateScreenshot = async (nextJob: BrowserJob) => {
    if (!nextJob.screenshot_url) return
    try {
      const nextScreenshot = await client.fetchScreenshot(nextJob.id)
      if (!nextScreenshot) return
      if (screenshotRef.current) URL.revokeObjectURL(screenshotRef.current)
      screenshotRef.current = nextScreenshot
      setScreenshot(nextScreenshot)
    } catch {
      // A progress update may arrive before the screenshot file is fully visible.
    }
  }

  const applyJobUpdate = (nextJob: BrowserJob) => {
    setJob(nextJob)
    void updateScreenshot(nextJob)
    if (nextJob.status === 'need_human' && nextJob.intervention) {
      setHumanMappings(Object.fromEntries(
        nextJob.intervention.field_candidates.map((candidateSet) => [
          candidateSet.canonical_field,
          candidateSet.candidates[0]?.element_id ?? '',
        ]),
      ))
      setHumanSubmitElement(
        nextJob.intervention.submission_candidates[0]?.element_id ?? '',
      )
      setHumanEntryElement(
        nextJob.intervention.entry_candidates[0]?.element_id ?? '',
      )
    }
    if (['completed', 'failed', 'cancelled', 'need_human'].includes(nextJob.status)) {
      setRunning(false)
    }
  }

  const connectToJob = (jobId: string) => {
    disconnectRef.current?.()
    disconnectRef.current = client.connectJobStream(jobId, applyJobUpdate)
  }

  const scanPageFields = async () => {
    setError('')
    setScanMessage('')
    setScanning(true)
    try {
      const target = new URL(form.targetUrl)
      const entryAliases = parseAliases(form.entryActionAliases)
      if (form.entryActionMode === 'click' && entryAliases.length === 0) {
        throw new Error('点击入口时至少需要一个入口按钮别名')
      }
      const result = await client.scanPage({
        target_url: target.toString(),
        entry_action: {
          mode: form.entryActionMode,
          aliases: entryAliases,
        },
      })
      if (result.fields.length === 0) {
        throw new Error('目标页面没有发现可填写字段')
      }
      setFields((current) => {
        const currentValues = new Map(current.map((field) => [field.key, field.value]))
        return result.fields.map((field, index) => ({
          id: `scanned-${index}-${field.key.replace(/[^A-Za-z0-9]/g, '-')}`,
          key: field.key,
          displayName: field.display_name,
          aliases: field.aliases.join(', '),
          inputKind: field.input_kind,
          sensitive: field.sensitive,
          sourceField: field.source_field ?? '',
          autocompleteHints: field.autocomplete_hints,
          value: currentValues.get(field.key) ?? '',
        }))
      })
      setShowFieldConfiguration(true)
      setScanMessage(`已从 ${result.final_url} 发现 ${result.fields.length} 个字段`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '扫描页面字段失败')
    } finally {
      setScanning(false)
    }
  }

  const buildCurrentWorkflowStep = (): WorkflowStepPayload => {
    const activeFields = fields.filter((field) => field.value.trim() || field.sourceField)
    if (activeFields.length === 0) throw new Error('请至少填写一个资料字段')
    const fieldKeyPattern = /^[a-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)+$/
    const keys = activeFields.map((field) => field.key.trim())
    if (keys.some((key) => !fieldKeyPattern.test(key))) {
      throw new Error('字段标识必须使用 person.firstName 这样的点分标识')
    }
    if (new Set(keys).size !== keys.length) throw new Error('字段标识不能重复')
    if (activeFields.some((field) => parseAliases(field.aliases).length === 0)) {
      throw new Error('每个字段至少需要一个页面别名')
    }
    const submissionAliases = parseAliases(form.submissionButtonAliases)
    if (form.submissionPolicy !== 'fill_only' && submissionAliases.length === 0) {
      throw new Error('提交策略启用时至少需要一个按钮别名')
    }
    const entryAliases = parseAliases(form.entryActionAliases)
    if (form.entryActionMode === 'click' && entryAliases.length === 0) {
      throw new Error('点击入口时至少需要一个入口按钮别名')
    }
    const activeKeySet = new Set(keys)
    if (activeFields.some((field) => field.sourceField && !activeKeySet.has(field.sourceField))) {
      throw new Error('来源字段必须填写数据并包含在当前步骤中')
    }
    return {
      name: form.stepName.trim() || `步骤 ${savedWorkflowSteps.length + 1}`,
      target_url: new URL(form.targetUrl).toString(),
      fields: Object.fromEntries(
        activeFields
          .filter((field) => !field.sourceField)
          .map((field) => [field.key.trim(), field.value]),
      ),
      field_definitions: activeFields.map((field) => ({
        key: field.key.trim(),
        display_name: field.displayName.trim(),
        aliases: parseAliases(field.aliases),
        input_kind: field.inputKind,
        sensitive: field.sensitive,
        source_field: field.sourceField || null,
        autocomplete_hints: field.autocompleteHints,
      })),
      submission: { policy: form.submissionPolicy, button_aliases: submissionAliases },
      entry_action: { mode: form.entryActionMode, aliases: entryAliases },
    }
  }

  const addWorkflowStep = () => {
    setError('')
    try {
      const step = buildCurrentWorkflowStep()
      setSavedWorkflowSteps((current) => [...current, step])
      setFields((current) => current.map((field) => ({ ...field, value: '' })))
      setForm((current) => ({
        ...current,
        stepName: `步骤 ${savedWorkflowSteps.length + 2}`,
        entryActionMode: 'direct',
        submissionPolicy: 'fill_only',
      }))
      setScanMessage('上一工作流步骤已保存，请配置下一步目标页面和字段')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '工作流步骤保存失败')
    }
  }

  const runTask = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    setRunning(true)
    disconnectRef.current?.()
    try {
      const currentStep = buildCurrentWorkflowStep()
      const workflowSteps = savedWorkflowSteps.length
        ? [...savedWorkflowSteps, currentStep]
        : []
      const target = new URL(workflowSteps[0]?.target_url ?? currentStep.target_url)
      const task = await client.createTask({
        name: form.taskName.trim(),
        target_origin: target.origin,
        record_count: 1,
      })
      await client.validateTask(task.id)
      await client.startTask(task.id)
      const createdJob = await client.createBrowserJob({
        task_id: task.id,
        target_url: target.toString(),
        fields: workflowSteps.length ? {} : currentStep.fields,
        field_definitions: workflowSteps.length ? [] : currentStep.field_definitions,
        submission: currentStep.submission,
        entry_action: currentStep.entry_action,
        workflow_steps: workflowSteps.length ? workflowSteps : undefined,
      })
      setJob(createdJob)
      connectToJob(createdJob.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '启动任务失败')
      setRunning(false)
    }
  }

  const resolveHumanIntervention = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!job?.intervention) return
    if (job.intervention.kind === 'entry_action_confirmation') {
      if (!humanEntryElement) {
        setError('请选择要点击的登录入口')
        return
      }
      setError('')
      setRunning(true)
      try {
        const resumed = await client.resolveBrowserJob(job.id, {
          field_mappings: {},
          approve_entry_action: true,
          entry_element_id: humanEntryElement,
        })
        setJob(resumed)
        connectToJob(job.id)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '登录入口确认失败')
        setRunning(false)
      }
      return
    }
    if (job.intervention.kind === 'submission_confirmation') {
      if (!humanSubmitElement) {
        setError('请选择要点击的提交按钮')
        return
      }
      setError('')
      setRunning(true)
      try {
        const resumed = await client.resolveBrowserJob(job.id, {
          field_mappings: {},
          approve_submission: true,
          submit_element_id: humanSubmitElement,
        })
        setJob(resumed)
        connectToJob(job.id)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '提交确认失败')
        setRunning(false)
      }
      return
    }
    const requiredFields = job.intervention.field_candidates
      .filter((candidateSet) => candidateSet.candidates.length > 0)
      .map((candidateSet) => candidateSet.canonical_field)
    if (requiredFields.some((field) => !humanMappings[field])) {
      setError('请为每个待确认字段选择一个候选控件')
      return
    }
    setError('')
    setRunning(true)
    try {
      const resumed = await client.resolveBrowserJob(job.id, {
        field_mappings: Object.fromEntries(
          requiredFields.map((field) => [field, humanMappings[field]]),
        ),
      })
      setJob(resumed)
      connectToJob(job.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '人工确认提交失败')
      setRunning(false)
    }
  }

  const cancelHumanIntervention = async () => {
    if (!job) return
    setError('')
    setRunning(true)
    try {
      const cancelled = await client.cancelBrowserJob(job.id)
      disconnectRef.current?.()
      disconnectRef.current = null
      setJob(cancelled)
      setRunning(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '终止任务失败')
      setRunning(false)
    }
  }

  const progress = job ? Math.round((job.completed_fields / job.total_fields) * 100) : 0

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">SF</span>
          <div><strong>SmartFill</strong><small>Semantic Browser Ops</small></div>
        </div>
        <nav aria-label="主导航">
          <button aria-label="执行控制台" className={`nav-item ${activeView === 'console' ? 'active' : ''}`} type="button" onClick={() => setActiveView('console')}><span>◉</span>执行控制台</button>
          <button aria-label="任务列表" className={`nav-item ${activeView === 'tasks' ? 'active' : ''}`} type="button" onClick={() => setActiveView('tasks')}><span>≡</span>任务列表</button>
          <button aria-label="用户导入" className={`nav-item ${activeView === 'import' ? 'active' : ''}`} type="button" onClick={() => setActiveView('import')}><span>⇧</span>用户导入</button>
          <button aria-label="系统设置" className={`nav-item ${activeView === 'settings' ? 'active' : ''}`} type="button" onClick={() => setActiveView('settings')}><span>⚙</span>系统设置</button>
        </nav>
        <div className="policy-card">
          <span className="signal" />
          <div><strong>安全策略已启用</strong><small>Origin 白名单 · 提交动作防重</small></div>
        </div>
      </aside>

      {activeView === 'tasks' && (
        <main className="workspace page-workspace">
          <header className="topbar">
            <div><p className="eyebrow">PERSISTED EXECUTIONS</p><h1>任务列表</h1></div>
          </header>
          {error && <div className="error-banner" role="alert">{error}</div>}
          <section className="panel history-panel">
            <div className="panel-heading"><div><p className="step">01 · RUNS</p><h2>执行记录</h2></div><span className="badge">{taskHistory.length} 条</span></div>
            <div className="task-history-list">
              {taskHistory.map((historyJob) => (
                <article className="task-history-row" key={historyJob.id}>
                  <div><strong>{historyJob.name ?? historyJob.task_id}</strong><small>{historyJob.total_steps ?? 1} 步 · {historyJob.completed_fields}/{historyJob.total_fields} 字段</small></div>
                  <span className={`status-chip ${historyJob.status}`}>{statusText[historyJob.status]}</span>
                  <time>{new Date(historyJob.created_at).toLocaleString('zh-CN')}</time>
                  <button
                    className="secondary-button"
                    type="button"
                    aria-label={`${selectedHistory?.id === historyJob.id ? '收起' : '查看'}任务 ${historyJob.name ?? historyJob.task_id}`}
                    aria-expanded={selectedHistory?.id === historyJob.id}
                    onClick={() => void openHistory(historyJob)}
                  >
                    {selectedHistory?.id === historyJob.id ? '收起详情' : '查看详情'}
                  </button>
                </article>
              ))}
              {!taskHistory.length && <div className="empty-timeline">暂无持久化任务</div>}
            </div>
          </section>
          {selectedHistory && (
            <section className="history-detail-grid">
              <article className="panel snapshot-panel">
                <div className="panel-heading"><div><p className="step">02 · SNAPSHOT</p><h2>配置快照</h2></div></div>
                <h3>{selectedHistory.name}</h3>
                {(selectedHistory.configuration_snapshot?.steps ?? []).map((step, index) => (
                  <div className="snapshot-step" key={step.id}>
                    <strong>{index + 1}. {step.name}</strong>
                    <small>{step.target_url}</small>
                    <span>{step.field_names.join('、')}</span>
                    <span>{submissionPolicyText[step.submission.policy]}</span>
                  </div>
                ))}
              </article>
              <article className="panel timeline-panel">
                <div className="panel-heading"><div><p className="step">03 · TIMELINE</p><h2>执行时间线</h2></div></div>
                <div className="timeline">
                  {[...selectedHistory.events].reverse().map((event) => (
                    <article key={event.sequence}>
                      <span className={`event-dot ${event.status}`} />
                      <div><strong>{event.message}</strong><small>{event.step_name ?? '任务'} · {new Date(event.created_at).toLocaleString('zh-CN')}</small></div>
                    </article>
                  ))}
                </div>
              </article>
            </section>
          )}
        </main>
      )}

      {activeView === 'import' && (
        <main className="workspace page-workspace">
          <header className="topbar">
            <div><p className="eyebrow">BATCH AUTOMATION</p><h1>用户数据批量执行</h1></div>
          </header>
          <div className="import-layout">
            <form className="panel import-panel" onSubmit={runBatch} noValidate>
              <div className="panel-heading">
                <div><p className="step">01 · IMPORT</p><h2>导入并选择工作流</h2></div>
                <span className="badge">CSV / XLSX</span>
              </div>
              <div className="form-grid">
                <label className="wide">批次名称<input value={importBatchName} onChange={(event) => setImportBatchName(event.target.value)} required /></label>
                <label className="wide">
                  任务工作流
                  <select value={importWorkflowId} onChange={(event) => {
                    setImportWorkflowId(event.target.value)
                    setImportMapping({})
                  }} required>
                    <option value="">请选择已有工作流</option>
                    {importWorkflows.map((workflow) => (
                      <option key={workflow.id} value={workflow.id}>
                        {workflow.name} · {workflow.total_steps ?? 1} 步
                      </option>
                    ))}
                  </select>
                </label>
                <label className="wide">
                  用户数据文件
                  <input
                    type="file"
                    accept=".csv,.xlsx"
                    onChange={(event) => void inspectImportFile(event.target.files?.[0] ?? null)}
                    required
                  />
                </label>
              </div>
              {importPreview && (
                <section className="mapping-panel" aria-label="导入字段映射">
                  <p className="scan-message" role="status">
                    已读取 {importPreview.filename}，共 {importPreview.total_rows} 条用户记录
                  </p>
                  {importFields.map((field) => (
                    <label key={field.key}>
                      {field.displayName} 数据列
                      <select
                        aria-label={`${field.displayName} 数据列`}
                        value={importMapping[field.key] ?? ''}
                        onChange={(event) => setImportMapping((current) => ({
                          ...current,
                          [field.key]: event.target.value,
                        }))}
                        required
                      >
                        <option value="">请选择数据列</option>
                        {importPreview.headers.map((header) => (
                          <option key={`${field.key}-${header}`} value={header}>{header}</option>
                        ))}
                      </select>
                    </label>
                  ))}
                </section>
              )}
              {error && <div className="error-banner" role="alert">{error}</div>}
              <button className="primary-button" type="submit" disabled={importing || !importPreview || !importWorkflowId}>
                {importing ? '正在创建批次…' : '开始批量执行'} <span>→</span>
              </button>
              <p className="form-note">批次按行顺序执行以控制浏览器负载；数据库仅保存字段映射和执行结果，不保存导入值明文。</p>
            </form>

            <section className="panel batch-panel">
              <div className="panel-heading"><div><p className="step">02 · BATCHES</p><h2>批次进度</h2></div><span className="badge">{batchHistory.length} 个</span></div>
              <div className="batch-list">
                {batchHistory.map((batch) => {
                  const processed = batch.completed_records + batch.failed_records
                  const percent = Math.round((processed / batch.total_records) * 100)
                  return (
                    <article key={batch.id} className="batch-row">
                      <div><strong>{batch.name}</strong><small>{batch.workflow_name} · {batch.source_filename}</small></div>
                      <span className={`status-chip ${batch.status}`}>{batchStatusText[batch.status]}</span>
                      <div className="progress-row"><span style={{ width: `${percent}%` }} /></div>
                      <small>{batch.completed_records}/{batch.total_records} 完成 · {batch.failed_records} 失败</small>
                    </article>
                  )
                })}
                {!batchHistory.length && <div className="empty-timeline">暂无批量执行记录</div>}
              </div>
            </section>
          </div>
        </main>
      )}

      {activeView === 'settings' && (
        <main className="workspace page-workspace">
          <header className="topbar"><div><p className="eyebrow">RUNTIME POLICY</p><h1>系统设置</h1></div></header>
          <section className="panel settings-panel">
            <div className="panel-heading"><div><p className="step">01 · ALLOWLIST</p><h2>目标网页白名单</h2></div><span className="badge">即时生效</span></div>
            <label>目标网页白名单<textarea value={originsText} onChange={(event) => setOriginsText(event.target.value)} rows={10} placeholder="每行一个 HTTPS Origin，例如 https://example.com" /></label>
            <p className="form-note">只填写 Origin，不包含路径。非本机地址必须使用 HTTPS；保存后立即影响页面扫描和后续工作流步骤。</p>
            {settingsMessage && <p className="scan-message" role="status">{settingsMessage}</p>}
            {error && <div className="error-banner" role="alert">{error}</div>}
            <button className="primary-button" type="button" onClick={() => void saveTargetOrigins()}>保存并立即生效</button>
          </section>
        </main>
      )}

      {activeView === 'console' && (<main className="workspace" id="console">
        <header className="topbar">
          <div>
            <p className="eyebrow">AUTOMATION WORKSPACE</p>
            <h1>浏览器自动化控制台</h1>
          </div>
          <label className="token-field">
            <span>API Token</span>
            <input
              type="password"
              value={apiToken}
              onChange={(event) => setApiToken(event.target.value)}
              onBlur={loadTargetOrigins}
              placeholder="仅保存在当前页面内存"
              autoComplete="off"
            />
          </label>
        </header>

        <section className="hero-strip">
          <div><span className="pulse" /><strong>Browser Worker</strong><small>Playwright / CDP ready</small></div>
          <div><strong>{job ? statusText[job.status] : '待命'}</strong><small>当前执行状态</small></div>
          <div><strong>{job ? `${job.completed_fields}/${job.total_fields}` : '0/0'}</strong><small>已验证字段</small></div>
          <div><strong>{submissionPolicyText[form.submissionPolicy]}</strong><small>提交策略</small></div>
        </section>

        <div className="content-grid">
          <form className="panel task-panel" onSubmit={runTask}>
            <div className="panel-heading">
              <div><p className="step">01 · CONFIGURE</p><h2>配置执行任务</h2></div>
              <span className="badge">{savedWorkflowSteps.length + 1} 个工作流步骤</span>
            </div>

            {savedWorkflowSteps.length > 0 && (
              <div className="workflow-step-list" aria-label="已保存工作流步骤">
                {savedWorkflowSteps.map((step, index) => (
                  <article key={`${step.name}-${index}`}>
                    <span>{index + 1}</span>
                    <div><strong>{step.name}</strong><small>{step.target_url} · {Object.keys(step.fields).length} 个字段</small></div>
                    <button type="button" aria-label={`删除工作流步骤 ${step.name}`} onClick={() => setSavedWorkflowSteps((current) => current.filter((_, itemIndex) => itemIndex !== index))}>删除</button>
                  </article>
                ))}
              </div>
            )}

            <div className="form-grid">
              <label className="wide">任务名称<input value={form.taskName} onChange={(e) => setField('taskName', e.target.value)} required /></label>
              <label className="wide">当前步骤名称<input value={form.stepName} onChange={(e) => setField('stepName', e.target.value)} required /></label>
              <label className="wide">
                目标页面 URL
                <select
                  value={targetUrlParts(form.targetUrl).origin}
                  onChange={(event) => setField(
                    'targetUrl',
                    `${event.target.value}${targetUrlParts(form.targetUrl).path}`,
                  )}
                  required
                >
                  {!targetOrigins.length && (
                    <option value={targetUrlParts(form.targetUrl).origin}>
                      {targetUrlParts(form.targetUrl).origin}
                    </option>
                  )}
                  {targetOrigins.map((origin) => (
                    <option key={origin} value={origin}>{origin}</option>
                  ))}
                </select>
              </label>
              <label className="wide">
                页面路径
                <input
                  value={targetUrlParts(form.targetUrl).path}
                  onChange={(event) => {
                    const path = event.target.value.startsWith('/')
                      ? event.target.value
                      : `/${event.target.value}`
                    setField('targetUrl', `${targetUrlParts(form.targetUrl).origin}${path}`)
                  }}
                  placeholder="/login"
                  required
                />
              </label>
              <label>
                进入表单方式
                <select
                  value={form.entryActionMode}
                  onChange={(event) => setField('entryActionMode', event.target.value as EntryActionMode)}
                >
                  <option value="direct">当前地址就是表单页</option>
                  <option value="click">先点击登录入口</option>
                </select>
              </label>
              {form.entryActionMode === 'click' && (
                <label>
                  入口按钮别名
                  <input
                    value={form.entryActionAliases}
                    onChange={(event) => setField('entryActionAliases', event.target.value)}
                    placeholder="登录, Login, Sign in"
                    required
                  />
                </label>
              )}
              <label>
                提交策略
                <select
                  value={form.submissionPolicy}
                  onChange={(event) => setField('submissionPolicy', event.target.value as SubmissionPolicy)}
                >
                  <option value="fill_only">仅填写，不点击提交</option>
                  <option value="confirm_before_submit">填写后人工确认提交</option>
                  <option value="auto_submit">唯一匹配时自动提交</option>
                </select>
              </label>
              {form.submissionPolicy !== 'fill_only' && (
                <label>
                  提交按钮别名
                  <input
                    value={form.submissionButtonAliases}
                    onChange={(event) => setField('submissionButtonAliases', event.target.value)}
                    placeholder="Register, 注册, 保存"
                    required
                  />
                </label>
              )}
              {fields.map((field) => (
                <div className="dynamic-field-entry" key={field.id}>
                  <label>
                    {field.displayName}
                    {field.key === 'person.gender' && field.inputKind === 'select' ? (
                      <select
                        value={field.value}
                        onChange={(event) => updateDynamicField(field.id, { value: event.target.value })}
                        disabled={Boolean(field.sourceField)}
                      >
                        <option value="">不填写</option>
                        <option value="male">男</option>
                        <option value="female">女</option>
                        <option value="other">其他</option>
                      </select>
                    ) : (
                      <input
                        value={field.value}
                        onChange={(event) => updateDynamicField(field.id, { value: event.target.value })}
                        type={field.sensitive ? 'password' : (field.inputKind === 'select' ? 'text' : field.inputKind)}
                        disabled={Boolean(field.sourceField)}
                        placeholder={field.sourceField ? `复制 ${field.sourceField}` : undefined}
                        autoComplete="off"
                      />
                    )}
                  </label>
                  {(field.id.startsWith('custom-') || field.id.startsWith('scanned-')) && (
                    <button
                      type="button"
                      className="inline-remove-field-button"
                      onClick={() => removeDynamicField(field.id)}
                      aria-label={`删除自定义字段 ${field.displayName}`}
                    >
                      删除字段
                    </button>
                  )}
                </div>
              ))}
            </div>

            <div className="field-config-actions">
              <button type="button" className="secondary-button workflow-button" disabled={running} onClick={addWorkflowStep}>保存当前步骤并添加下一步</button>
              <button
                type="button"
                className="secondary-button scan-button"
                disabled={scanning || running}
                onClick={scanPageFields}
              >
                {scanning ? '正在扫描…' : '扫描页面字段'}
              </button>
              <button type="button" className="secondary-button" onClick={addDynamicField}>
                添加自定义字段
              </button>
              <button
                type="button"
                className="secondary-button"
                aria-expanded={showFieldConfiguration}
                onClick={() => setShowFieldConfiguration((current) => !current)}
              >
                {showFieldConfiguration ? '收起字段映射' : '配置字段映射'}
              </button>
            </div>

            {scanMessage && <p className="scan-message" role="status">{scanMessage}</p>}

            {showFieldConfiguration && (
              <section className="field-config-panel" aria-label="动态字段映射配置">
                <p>字段标识对应提交数据；页面别名用于匹配 label、ARIA、placeholder 和 name。</p>
                {fields.map((field) => (
                  <fieldset
                    key={`${field.id}-configuration`}
                    className="field-config-row"
                    aria-label={`${field.displayName} 字段配置`}
                  >
                    <label>
                      字段标识
                      <input
                        value={field.key}
                        onChange={(event) => updateDynamicField(field.id, { key: event.target.value })}
                      />
                    </label>
                    <label>
                      显示名称
                      <input
                        value={field.displayName}
                        onChange={(event) => updateDynamicField(field.id, { displayName: event.target.value })}
                      />
                    </label>
                    <label className="wide">
                      页面别名
                      <input
                        value={field.aliases}
                        onChange={(event) => updateDynamicField(field.id, { aliases: event.target.value })}
                        placeholder="使用逗号分隔"
                      />
                    </label>
                    <label>
                      数据类型
                      <select
                        value={field.inputKind}
                        onChange={(event) => {
                          const inputKind = event.target.value as FieldInputKind
                          updateDynamicField(field.id, {
                            inputKind,
                            sensitive: inputKind === 'password' ? true : field.sensitive,
                          })
                        }}
                      >
                        <option value="text">文本</option>
                        <option value="password">密码</option>
                        <option value="email">邮箱</option>
                        <option value="tel">电话</option>
                        <option value="select">下拉选择</option>
                      </select>
                    </label>
                    <label>
                      来源字段
                      <select
                        value={field.sourceField}
                        onChange={(event) => updateDynamicField(field.id, {
                          sourceField: event.target.value,
                          sensitive: event.target.value ? true : field.sensitive,
                        })}
                      >
                        <option value="">手动填写</option>
                        {fields.filter((candidate) => candidate.id !== field.id).map((candidate) => (
                          <option key={`${field.id}-${candidate.id}-source`} value={candidate.key}>
                            {candidate.displayName} · {candidate.key}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={field.sensitive}
                        disabled={field.inputKind === 'password' || Boolean(field.sourceField)}
                        onChange={(event) => updateDynamicField(field.id, {
                          sensitive: event.target.checked,
                        })}
                      />
                      敏感字段
                    </label>
                    <button
                      type="button"
                      className="remove-field-button"
                      onClick={() => removeDynamicField(field.id)}
                      aria-label={`从映射配置删除 ${field.displayName}`}
                    >
                      删除
                    </button>
                  </fieldset>
                ))}
              </section>
            )}

            {error && <div className="error-banner" role="alert">{error}</div>}
            <button className="primary-button" type="submit" disabled={running}>
              {running ? '浏览器执行中…' : '启动浏览器填写'} <span>→</span>
            </button>
            <p className="form-note">
              {form.submissionPolicy === 'fill_only'
                ? '仅填写并回读验证，不会点击提交按钮。'
                : '提交按钮按可访问名称和页面语义匹配，每个任务最多尝试点击一次。'}
              密码和证件号进入 Worker 前会转换为密钥引用。
            </p>
          </form>

          <section className="panel monitor-panel">
            <div className="panel-heading">
              <div><p className="step">02 · OBSERVE</p><h2>浏览器实时画面</h2></div>
              <span className={`status-chip ${job?.status ?? 'idle'}`}>{job ? statusText[job.status] : '尚未启动'}</span>
            </div>
            <div className="browser-frame">
              <div className="browser-chrome"><i /><i /><i /><span>{job?.current_url ?? form.targetUrl}</span></div>
              {screenshot ? <img src={screenshot} alt="Browser Worker 脱敏截图" /> : <div className="empty-screen"><span>⌁</span><strong>等待浏览器画面</strong><small>启动任务后显示经过脱敏的最新截图</small></div>}
            </div>
            <div className="progress-row"><span style={{ width: `${progress}%` }} /></div>
            <div className="job-summary">
              <div><small>JOB ID</small><strong>{job?.id.slice(0, 8) ?? '—'}</strong></div>
              <div><small>当前字段</small><strong>{job?.current_field ?? '—'}</strong></div>
              <div><small>完成度</small><strong>{progress}%</strong></div>
            </div>
          </section>

          {job?.status === 'need_human' && job.intervention && (
            <section className="panel human-panel" aria-labelledby="human-confirmation-title">
              <div className="panel-heading">
                <div>
                  <p className="step">03 · HUMAN GATE</p>
                  <h2 id="human-confirmation-title">需要人工确认</h2>
                </div>
                <span className="status-chip need_human">会话已保留</span>
              </div>
              <p className="human-instruction">{job.intervention.instruction}</p>
              {job.intervention.requires_browser_interaction && (
                <p className="human-browser-note" role="note">
                  请在可见 Chromium 或已连接的 CDP 浏览器中完成人机验证，系统不会尝试破解验证码。
                </p>
              )}
              <form className="human-form" onSubmit={resolveHumanIntervention}>
                {job.intervention.kind === 'entry_action_confirmation' && (
                  <label>
                    <span>登录入口候选</span>
                    <select
                      aria-label="登录入口候选"
                      value={humanEntryElement}
                      onChange={(event) => setHumanEntryElement(event.target.value)}
                      required
                    >
                      <option value="">请选择</option>
                      {job.intervention.entry_candidates.map((candidate) => (
                        <option key={candidate.element_id} value={candidate.element_id}>
                          {candidate.accessible_name || candidate.element_id}
                          {' · '}{candidate.frame_path}
                          {' · '}{Math.round(candidate.confidence * 100)}%
                        </option>
                      ))}
                    </select>
                    <small>只能选择本次首页扫描发现的链接或按钮。</small>
                  </label>
                )}
                {job.intervention.kind === 'submission_confirmation' && (
                  <label>
                    <span>提交按钮候选</span>
                    <select
                      aria-label="提交按钮候选"
                      value={humanSubmitElement}
                      onChange={(event) => setHumanSubmitElement(event.target.value)}
                      required
                    >
                      <option value="">请选择</option>
                      {job.intervention.submission_candidates.map((candidate) => (
                        <option key={candidate.element_id} value={candidate.element_id}>
                          {candidate.accessible_name || candidate.element_id}
                          {' · '}{candidate.frame_path}
                          {' · '}{Math.round(candidate.confidence * 100)}%
                        </option>
                      ))}
                    </select>
                    <small>仅允许点击本次页面快照中的候选按钮，不接受 CSS 或 XPath。</small>
                  </label>
                )}
                {job.intervention.field_candidates.map((candidateSet) => (
                  <label key={candidateSet.canonical_field}>
                    <span>{candidateSet.canonical_field} 候选控件</span>
                    <select
                      aria-label={`${candidateSet.canonical_field} 候选控件`}
                      value={humanMappings[candidateSet.canonical_field] ?? ''}
                      onChange={(event) => setHumanMappings((current) => ({
                        ...current,
                        [candidateSet.canonical_field]: event.target.value,
                      }))}
                      required={candidateSet.candidates.length > 0}
                    >
                      <option value="">请选择</option>
                      {candidateSet.candidates.map((candidate) => (
                        <option key={candidate.element_id} value={candidate.element_id}>
                          {candidate.accessible_name || candidate.element_id}
                          {' · '}{candidate.role || candidate.tag}
                          {' · '}{candidate.frame_path}
                        </option>
                      ))}
                    </select>
                    {candidateSet.candidates.map((candidate) => (
                      <small key={`${candidate.element_id}-path`}>
                        {candidate.accessible_name || candidate.element_id} — {candidate.frame_path}
                      </small>
                    ))}
                  </label>
                ))}
                <div className="human-actions">
                  <button
                    className="secondary-danger-button"
                    type="button"
                    disabled={running}
                    onClick={cancelHumanIntervention}
                  >
                    终止任务
                  </button>
                  <button className="primary-button human-confirm-button" type="submit" disabled={running}>
                    {running
                      ? '正在继续执行…'
                      : (job.intervention.kind === 'submission_confirmation'
                          ? '确认并提交一次'
                          : (job.intervention.kind === 'entry_action_confirmation'
                              ? '确认并进入登录页'
                              : '确认并重新扫描'))}
                  </button>
                </div>
              </form>
            </section>
          )}

          <section className="panel timeline-panel" id="events">
            <div className="panel-heading"><div><p className="step">04 · AUDIT</p><h2>动作时间线</h2></div></div>
            <div className="timeline">
              {job?.events.length ? [...job.events].reverse().map((event) => (
                <article key={event.sequence}>
                  <span className={`event-dot ${event.status}`} />
                  <div><strong>{event.message}</strong><small>{event.field ?? statusText[event.status]} · {new Date(event.created_at).toLocaleTimeString('zh-CN')}</small></div>
                </article>
              )) : <div className="empty-timeline">任务事件将在这里实时出现</div>}
            </div>
            {job && <div className={`result-banner ${job.status}`}><strong>{job.message}</strong><span>{job.completed_fields}/{job.total_fields} 字段已验证</span></div>}
          </section>
        </div>
      </main>)}
    </div>
  )
}
