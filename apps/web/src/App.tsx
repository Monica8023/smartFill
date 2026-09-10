import { useEffect, useMemo, useRef, useState } from 'react'

import {
  HttpSmartFillClient,
  type BatchRun,
  type AuthenticationMode,
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
  entryActionMode: EntryActionMode
  authenticationMode: AuthenticationMode
  targetIntent: string
  observationIntervalSeconds: number
  authenticationSessionKey: string
  heartbeatUrl: string
  heartbeatIntervalSeconds: number
}

interface EditableField {
  id: string
  key: string
  displayName: string
  inputKind: FieldInputKind
  sensitive: boolean
  sourceField: string
  autocompleteHints: string[]
  value: string
  required?: boolean
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
  resuming: '继续视觉识别',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已取消',
}

const terminalJobStatuses: BrowserJobStatus[] = ['completed', 'failed', 'cancelled']

function defaultTargetUrl(): string {
  if (window.location.port === '8000') return `${window.location.origin}/demo/target`
  return `${window.location.protocol}//${window.location.hostname}:8000/demo/target`
}

const initialForm: FormState = {
  taskName: '资料页自动填写演示',
  stepName: '填写资料',
  targetUrl: defaultTargetUrl(),
  submissionPolicy: 'fill_only',
  entryActionMode: 'auto',
  authenticationMode: 'none',
  targetIntent: '找到目标业务入口并填写相关资料',
  observationIntervalSeconds: 5,
  authenticationSessionKey: '',
  heartbeatUrl: '',
  heartbeatIntervalSeconds: 300,
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
    'account.email': '账号邮箱',
    'account.password': '密码',
    'account.passwordConfirmation': '确认密码',
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
  { id: 'account-username', key: 'account.username', displayName: '用户名', inputKind: 'text', sensitive: false, sourceField: '', autocompleteHints: ['username'], value: '' },
  { id: 'account-password', key: 'account.password', displayName: '登录密码', inputKind: 'password', sensitive: true, sourceField: '', autocompleteHints: ['current-password', 'new-password'], value: '' },
  { id: 'person-full-name', key: 'person.fullName', displayName: '姓名', inputKind: 'text', sensitive: false, sourceField: '', autocompleteHints: ['name'], value: '' },
  { id: 'person-gender', key: 'person.gender', displayName: '性别', inputKind: 'select', sensitive: false, sourceField: '', autocompleteHints: [], value: '' },
  { id: 'person-id-number', key: 'person.idNumber', displayName: '身份证号', inputKind: 'text', sensitive: true, sourceField: '', autocompleteHints: [], value: '' },
  { id: 'person-phone', key: 'person.phone', displayName: '手机号', inputKind: 'tel', sensitive: true, sourceField: '', autocompleteHints: ['tel', 'tel-national'], value: '' },
  { id: 'person-email', key: 'person.email', displayName: '邮箱', inputKind: 'email', sensitive: false, sourceField: '', autocompleteHints: ['email'], value: '' },
  { id: 'person-address', key: 'person.address', displayName: '联系地址', inputKind: 'text', sensitive: false, sourceField: '', autocompleteHints: ['street-address', 'address-line1'], value: '' },
]

function parseAliases(value: string): string[] {
  return [...new Set(value.split(/[,，\n]/).map((alias) => alias.trim()).filter(Boolean))]
}

type SnapshotStep = NonNullable<
  NonNullable<BrowserJob['configuration_snapshot']>['steps']
>[number]

function fallbackFieldDefinition(key: string): FieldDefinition {
  const sensitive = /(password|idNumber|phone)$/i.test(key)
  const inputKind: FieldInputKind = /password$/i.test(key)
    ? 'password'
    : /email$/i.test(key)
      ? 'email'
      : /phone$/i.test(key)
        ? 'tel'
        : 'text'
  return {
    key,
    display_name: fallbackFieldName(key),
    aliases: [fallbackFieldName(key), key],
    input_kind: inputKind,
    sensitive,
    source_field: null,
    autocomplete_hints: [],
  }
}

function reusableStepFromSnapshot(step: SnapshotStep): WorkflowStepPayload {
  const definitions = step.field_definitions.length
    ? step.field_definitions
    : step.field_names.map(fallbackFieldDefinition)
  return {
    id: step.id,
    name: step.name,
    target_url: step.target_url,
    fields: Object.fromEntries(definitions
      .filter((definition) => !definition.source_field)
      .map((definition) => [
        definition.key,
        definition.sensitive ? '' : (step.field_values?.[definition.key] ?? ''),
      ])),
    field_definitions: definitions,
    entry_action: step.entry_action,
    submission: step.submission,
    target_intent: step.target_intent ?? '',
    authentication_mode: step.authentication_mode ?? 'none',
    observation_interval_seconds: step.observation_interval_seconds ?? 5,
    authentication_session_key: step.authentication_session_key ?? null,
    heartbeat_url: step.heartbeat_url ?? null,
    heartbeat_interval_seconds: step.heartbeat_interval_seconds ?? 300,
  }
}

function editableFieldsFromWorkflowStep(step: WorkflowStepPayload): EditableField[] {
  const definitions = step.field_definitions ?? Object.keys(step.fields).map(fallbackFieldDefinition)
  return definitions.map((definition, index) => ({
    id: `reused-${index}-${definition.key.replace(/[^A-Za-z0-9]/g, '-')}`,
    key: definition.key,
    displayName: definition.display_name,
    inputKind: definition.input_kind,
    sensitive: definition.sensitive,
    sourceField: definition.source_field ?? '',
    autocompleteHints: definition.autocomplete_hints,
    value: definition.source_field ? '' : (step.fields[definition.key] ?? ''),
    required: !definition.source_field
      && Object.prototype.hasOwnProperty.call(step.fields, definition.key),
  }))
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
  const [cancellingJobId, setCancellingJobId] = useState<string | null>(null)
  const [closingBrowser, setClosingBrowser] = useState(false)
  const [workflowMessage, setWorkflowMessage] = useState('')
  const [error, setError] = useState('')
  const [humanSubmitElement, setHumanSubmitElement] = useState('')
  const [humanEntryElement, setHumanEntryElement] = useState('')
  const [missingFieldValues, setMissingFieldValues] = useState<Record<string, string>>({})
  const [taskHistory, setTaskHistory] = useState<BrowserJob[]>([])
  const [selectedHistory, setSelectedHistory] = useState<BrowserJob | null>(null)
  const [originsText, setOriginsText] = useState('')
  const [targetOrigins, setTargetOrigins] = useState<string[]>([])
  const [settingsMessage, setSettingsMessage] = useState('')
  const [savedWorkflowSteps, setSavedWorkflowSteps] = useState<WorkflowStepPayload[]>([])
  const [editingWorkflowStepIndex, setEditingWorkflowStepIndex] = useState<number | null>(null)
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

  const loadWorkflowStepDraft = (step: WorkflowStepPayload) => {
    setForm((current) => ({
      ...current,
      stepName: step.name,
      targetUrl: step.target_url,
      submissionPolicy: step.submission?.policy ?? 'fill_only',
      entryActionMode: step.entry_action?.mode === 'direct' ? 'direct' : 'auto',
      authenticationMode: step.authentication_mode ?? 'none',
      targetIntent: step.target_intent?.trim()
        || '找到当前步骤的目标业务表单并填写预设资料',
      observationIntervalSeconds: step.observation_interval_seconds ?? 5,
      authenticationSessionKey: step.authentication_session_key ?? '',
      heartbeatUrl: step.heartbeat_url ?? '',
      heartbeatIntervalSeconds: step.heartbeat_interval_seconds ?? 300,
    }))
    setFields(editableFieldsFromWorkflowStep(step))
    setShowFieldConfiguration(true)
  }

  const reuseHistory = async (historyJob: BrowserJob) => {
    setError('')
    try {
      const detail = await client.getJob(historyJob.id)
      const snapshotSteps = detail.configuration_snapshot?.steps ?? []
      if (!snapshotSteps.length) throw new Error('该任务没有可复用的配置快照')
      const reusableSteps = snapshotSteps.map(reusableStepFromSnapshot)
      setSavedWorkflowSteps(reusableSteps)
      setEditingWorkflowStepIndex(0)
      setForm((current) => ({
        ...current,
        taskName: `${detail.name ?? detail.task_id}（复用）`,
      }))
      loadWorkflowStepDraft(reusableSteps[0])
      disconnectRef.current?.()
      setJob(null)
      setSelectedHistory(null)
      setRunning(false)
      setHumanSubmitElement('')
      setHumanEntryElement('')
      setMissingFieldValues({})
      if (screenshotRef.current) URL.revokeObjectURL(screenshotRef.current)
      screenshotRef.current = null
      setScreenshot(null)
      setWorkflowMessage('配置已回填；密码、证件号、手机号等敏感字段不会从历史记录恢复，请重新填写')
      setActiveView('console')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '任务复用失败')
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
      setMissingFieldValues(Object.fromEntries(
        (nextJob.intervention.missing_fields ?? []).map((field) => [field.key, '']),
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

  const buildCurrentWorkflowStep = (validateRequired = true): WorkflowStepPayload => {
    const missingRequiredFields = fields.filter((field) => (
      field.required && !field.sourceField && !field.value.trim()
    ))
    if (validateRequired && missingRequiredFields.length) {
      throw new Error(
        `复用步骤“${form.stepName}”需要重新填写：${missingRequiredFields.map((field) => field.displayName).join('、')}`,
      )
    }
    const activeFields = fields.filter((field) => (
      field.value.trim() || field.sourceField || (!validateRequired && field.required)
    ))
    if (!form.targetIntent.trim()) throw new Error('请填写目标任务描述')
    if (form.authenticationMode === 'manual') {
      if (!form.authenticationSessionKey.trim() || !form.heartbeatUrl.trim()) {
        throw new Error('人工登录需要填写会话标识和心跳 URL')
      }
      const heartbeatUrl = new URL(form.heartbeatUrl)
      if (heartbeatUrl.search || heartbeatUrl.hash) {
        throw new Error('登录心跳 URL 不能包含查询参数或片段')
      }
      if (heartbeatUrl.origin !== new URL(form.targetUrl).origin) {
        throw new Error('登录心跳 URL 必须与目标页面同源')
      }
    }
    const fieldKeyPattern = /^[a-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)+$/
    const keys = activeFields.map((field) => field.key.trim())
    if (keys.some((key) => !fieldKeyPattern.test(key))) {
      throw new Error('字段标识必须使用 person.firstName 这样的点分标识')
    }
    if (new Set(keys).size !== keys.length) throw new Error('字段标识不能重复')
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
        aliases: [field.displayName.trim(), field.key.trim()],
        input_kind: field.inputKind,
        sensitive: field.sensitive,
        source_field: field.sourceField || null,
        autocomplete_hints: field.autocompleteHints,
      })),
      submission: { policy: form.submissionPolicy, button_aliases: [] },
      entry_action: { mode: form.entryActionMode, aliases: [] },
      target_intent: form.targetIntent.trim(),
      authentication_mode: form.authenticationMode,
      observation_interval_seconds: form.observationIntervalSeconds,
      authentication_session_key: form.authenticationMode === 'manual'
        ? form.authenticationSessionKey.trim()
        : null,
      heartbeat_url: form.authenticationMode === 'manual' ? form.heartbeatUrl.trim() : null,
      heartbeat_interval_seconds: form.heartbeatIntervalSeconds,
    }
  }

  const addWorkflowStep = () => {
    setError('')
    try {
      const step = buildCurrentWorkflowStep()
      const nextSteps = editingWorkflowStepIndex === null
        ? [...savedWorkflowSteps, step]
        : savedWorkflowSteps.map((item, index) => (
          index === editingWorkflowStepIndex ? step : item
        ))
      setSavedWorkflowSteps(nextSteps)
      setEditingWorkflowStepIndex(null)
      setFields((current) => current.map((field) => ({ ...field, value: '', required: false })))
      setForm((current) => ({
        ...current,
        stepName: `步骤 ${nextSteps.length + 1}`,
        entryActionMode: 'auto',
        submissionPolicy: 'fill_only',
        authenticationMode: 'none',
        targetIntent: '找到当前步骤的目标业务表单并填写预设资料',
        observationIntervalSeconds: 5,
        authenticationSessionKey: '',
        heartbeatUrl: '',
        heartbeatIntervalSeconds: 300,
      }))
      setWorkflowMessage('上一工作流步骤已保存，请配置下一步目标页面和字段')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '工作流步骤保存失败')
    }
  }

  const editWorkflowStep = (index: number) => {
    if (editingWorkflowStepIndex === index) return
    setError('')
    try {
      const nextSteps = [...savedWorkflowSteps]
      if (editingWorkflowStepIndex !== null) {
        nextSteps[editingWorkflowStepIndex] = buildCurrentWorkflowStep(false)
      } else if (fields.some((field) => field.value.trim() || field.sourceField)) {
        nextSteps.push(buildCurrentWorkflowStep())
      }
      const targetStep = nextSteps[index]
      if (!targetStep) throw new Error('工作流步骤不存在')
      setSavedWorkflowSteps(nextSteps)
      setEditingWorkflowStepIndex(index)
      loadWorkflowStepDraft(targetStep)
      setWorkflowMessage(`正在编辑第 ${index + 1} 步：${targetStep.name}`)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '工作流步骤切换失败')
    }
  }

  const removeWorkflowStep = (index: number) => {
    setSavedWorkflowSteps((current) => current.filter((_, itemIndex) => itemIndex !== index))
    setEditingWorkflowStepIndex((current) => {
      if (current === null) return null
      if (current === index) return null
      return current > index ? current - 1 : current
    })
  }

  const runTask = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    setRunning(true)
    disconnectRef.current?.()
    try {
      const currentStep = buildCurrentWorkflowStep()
      const configuredSteps = editingWorkflowStepIndex === null
        ? [...savedWorkflowSteps, currentStep]
        : savedWorkflowSteps.map((step, index) => (
          index === editingWorkflowStepIndex ? currentStep : step
        ))
      for (const step of configuredSteps) {
        const missing = (step.field_definitions ?? [])
          .filter((definition) => (
            !definition.source_field
            && Object.prototype.hasOwnProperty.call(step.fields, definition.key)
            && !step.fields[definition.key].trim()
          ))
        if (missing.length) {
          throw new Error(
            `复用步骤“${step.name}”需要重新填写：${missing.map((definition) => definition.display_name).join('、')}`,
          )
        }
      }
      const workflowSteps = configuredSteps.length > 1 ? configuredSteps : []
      const target = new URL(configuredSteps[0]?.target_url ?? currentStep.target_url)
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
        target_intent: currentStep.target_intent,
        authentication_mode: currentStep.authentication_mode,
        observation_interval_seconds: currentStep.observation_interval_seconds,
        authentication_session_key: currentStep.authentication_session_key,
        heartbeat_url: currentStep.heartbeat_url,
        heartbeat_interval_seconds: currentStep.heartbeat_interval_seconds,
        workflow_steps: workflowSteps.length ? workflowSteps : undefined,
        keep_browser_open: true,
      })
      setJob(createdJob)
      if (createdJob.task_id !== task.id) {
        setWorkflowMessage('相同登录会话已有任务正在执行，已切换到现有任务和登录窗口')
      }
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
        setError('请选择要点击的表单入口')
        return
      }
      setError('')
      setRunning(true)
      try {
        const resumed = await client.resolveBrowserJob(job.id, {
          approve_entry_action: true,
          entry_element_id: humanEntryElement,
        })
        setJob(resumed)
        connectToJob(job.id)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '表单入口确认失败')
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
    if (job.intervention.kind === 'data_required') {
      const requiredFields = job.intervention.missing_fields ?? []
      if (requiredFields.some((field) => !missingFieldValues[field.key]?.trim())) {
        setError('请补齐全部缺失资料后继续')
        return
      }
      setError('')
      setRunning(true)
      try {
        const resumed = await client.resolveBrowserJob(job.id, {
          field_values: Object.fromEntries(
            requiredFields.map((field) => [field.key, missingFieldValues[field.key].trim()]),
          ),
        })
        setJob(resumed)
        connectToJob(job.id)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '补充资料失败')
        setRunning(false)
      }
      return
    }
    if (job.intervention.kind === 'manual_login') {
      setError('')
      setRunning(true)
      try {
        const resumed = await client.resolveBrowserJob(job.id, {
          manual_login_completed: true,
        })
        setJob(resumed)
        connectToJob(job.id)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : '人工登录确认失败')
        setRunning(false)
      }
      return
    }
    setError('')
    setRunning(true)
    try {
      const resumed = await client.resolveBrowserJob(job.id, {})
      setJob(resumed)
      connectToJob(job.id)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '人工确认提交失败')
      setRunning(false)
    }
  }

  const cancelJob = async (targetJob: BrowserJob) => {
    setError('')
    setCancellingJobId(targetJob.id)
    try {
      const cancelled = await client.cancelBrowserJob(targetJob.id)
      setTaskHistory((current) => current.map((item) => (
        item.id === cancelled.id ? cancelled : item
      )))
      setSelectedHistory((current) => current?.id === cancelled.id ? cancelled : current)
      if (job?.id === cancelled.id) {
        disconnectRef.current?.()
        disconnectRef.current = null
        setJob(cancelled)
        setRunning(false)
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '终止任务失败')
    } finally {
      setCancellingJobId(null)
    }
  }

  const closeCompletedBrowser = async () => {
    if (!job?.browser_session_open) return
    setError('')
    setClosingBrowser(true)
    try {
      const closed = await client.closeBrowserJob(job.id)
      setJob(closed)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '关闭目标浏览器失败')
    } finally {
      setClosingBrowser(false)
    }
  }

  const progress = job ? Math.round((job.completed_fields / job.total_fields) * 100) : 0
  const canCancelCurrentJob = Boolean(job && !terminalJobStatuses.includes(job.status))

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
                  {!terminalJobStatuses.includes(historyJob.status) && (
                    <button
                      className="task-stop-button"
                      type="button"
                      aria-label={`终止任务 ${historyJob.name ?? historyJob.task_id}`}
                      disabled={cancellingJobId === historyJob.id}
                      onClick={() => void cancelJob(historyJob)}
                    >
                      {cancellingJobId === historyJob.id ? '终止中…' : '终止'}
                    </button>
                  )}
                  <button
                    className="secondary-button reuse-button"
                    type="button"
                    aria-label={`复用任务 ${historyJob.name ?? historyJob.task_id}`}
                    onClick={() => void reuseHistory(historyJob)}
                  >
                    复用
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
            <p className="form-note">只填写 Origin，不包含路径。非本机地址必须使用 HTTPS；保存后立即影响视觉执行和后续工作流步骤。</p>
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
              <span className="badge">
                {editingWorkflowStepIndex === null ? savedWorkflowSteps.length + 1 : savedWorkflowSteps.length} 个工作流步骤
              </span>
            </div>

            {savedWorkflowSteps.length > 0 && (
              <div className="workflow-step-list" aria-label="已保存工作流步骤">
                {savedWorkflowSteps.map((step, index) => (
                  <article className={editingWorkflowStepIndex === index ? 'active' : ''} key={`${step.name}-${index}`}>
                    <span>{index + 1}</span>
                    <div><strong>{step.name}</strong><small>{step.target_url} · {Object.keys(step.fields).length} 个字段</small></div>
                    <button
                      type="button"
                      className="edit-step-button"
                      aria-label={`编辑工作流步骤 ${step.name}`}
                      disabled={editingWorkflowStepIndex === index}
                      onClick={() => editWorkflowStep(index)}
                    >
                      {editingWorkflowStepIndex === index ? '编辑中' : '编辑'}
                    </button>
                    <button
                      type="button"
                      aria-label={`删除工作流步骤 ${step.name}`}
                      disabled={editingWorkflowStepIndex === index}
                      onClick={() => removeWorkflowStep(index)}
                    >
                      删除
                    </button>
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
                  {!targetOrigins.includes(targetUrlParts(form.targetUrl).origin) && (
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
                账户流程
                <select
                  value={form.authenticationMode}
                  onChange={(event) => setField(
                    'authenticationMode',
                    event.target.value as AuthenticationMode,
                  )}
                >
                  <option value="none">不处理登录或注册</option>
                  <option value="login">登录已有账户</option>
                  <option value="register">注册新账户</option>
                  <option value="manual">人工登录并复用会话</option>
                </select>
              </label>
              {form.authenticationMode === 'manual' && (
                <>
                  <label>
                    登录会话标识
                    <input
                      value={form.authenticationSessionKey}
                      onChange={(event) => setField('authenticationSessionKey', event.target.value)}
                      placeholder="例如：房产业务账号"
                      required
                    />
                  </label>
                  <label className="wide">
                    登录心跳 URL
                    <input
                      aria-label="登录心跳 URL"
                      type="url"
                      value={form.heartbeatUrl}
                      onChange={(event) => setField('heartbeatUrl', event.target.value)}
                      placeholder={`${targetUrlParts(form.targetUrl).origin}/session/heartbeat`}
                      required
                    />
                    <small>必须是同一站点内安全、只读且能刷新登录态的 GET 地址。</small>
                  </label>
                  <label>
                    心跳间隔（秒）
                    <input
                      type="number"
                      min="30"
                      max="3600"
                      value={form.heartbeatIntervalSeconds}
                      onChange={(event) => setForm((current) => ({
                        ...current,
                        heartbeatIntervalSeconds: Number(event.target.value) || 300,
                      }))}
                    />
                  </label>
                </>
              )}
              <label>
                截图理解频率（秒）
                <input
                  type="number"
                  min="1"
                  max="30"
                  value={form.observationIntervalSeconds}
                  onChange={(event) => setForm((current) => ({
                    ...current,
                    observationIntervalSeconds: Number(event.target.value) || 5,
                  }))}
                />
              </label>
              <label className="wide">
                目标任务描述
                <textarea
                  aria-label="目标任务描述"
                  value={form.targetIntent}
                  onChange={(event) => setField('targetIntent', event.target.value)}
                  placeholder="例如：找到填写房产认证信息的入口并填写资料"
                  required
                />
                <small>Agent 会按此目标切换菜单，并按设定频率重新截图理解页面。</small>
              </label>
              <label>
                进入表单方式
                <select
                  value={form.entryActionMode}
                  onChange={(event) => setField('entryActionMode', event.target.value as EntryActionMode)}
                >
                  <option value="auto">视觉模型规划入口</option>
                  <option value="direct">当前地址就是表单页</option>
                </select>
              </label>
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
                        required={Boolean(field.required && !field.sourceField)}
                        placeholder={field.sourceField
                          ? `复制 ${field.sourceField}`
                          : (field.sensitive && field.required ? '历史敏感值不会回填，请重新输入' : undefined)}
                        autoComplete="off"
                      />
                    )}
                  </label>
                  {field.id.startsWith('custom-') && (
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
              <button type="button" className="secondary-button" onClick={addDynamicField}>
                添加自定义字段
              </button>
              <button
                type="button"
                className="secondary-button"
                aria-expanded={showFieldConfiguration}
                onClick={() => setShowFieldConfiguration((current) => !current)}
              >
                {showFieldConfiguration ? '收起数据字段' : '配置数据字段'}
              </button>
            </div>

            {workflowMessage && <p className="scan-message" role="status">{workflowMessage}</p>}

            {showFieldConfiguration && (
              <section className="field-config-panel" aria-label="业务数据字段配置">
                <p>这里只描述业务数据意图；目标网页中的控件由视觉模型在执行时识别。</p>
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
            <button
              className="primary-button"
              type="submit"
              disabled={running || Boolean(job && !['completed', 'failed', 'cancelled'].includes(job.status))}
            >
              {running ? '浏览器执行中…' : '启动浏览器填写'} <span>→</span>
            </button>
            <p className="form-note">
              {form.submissionPolicy === 'fill_only'
                ? '仅填写并回读验证，不会点击提交按钮。'
                : '提交按钮由视觉模型识别并经过动作门禁，每个任务最多尝试点击一次。'}
              密码和证件号进入 Worker 前会转换为密钥引用。控制台任务完成后会保留目标浏览器，
              请在结果区手动关闭。
            </p>
          </form>

          <section className="panel monitor-panel">
            <div className="panel-heading">
              <div><p className="step">02 · OBSERVE</p><h2>浏览器实时画面</h2></div>
              <div className="monitor-controls">
                <span className={`status-chip ${job?.status ?? 'idle'}`}>{job ? statusText[job.status] : '尚未启动'}</span>
                {canCancelCurrentJob && (
                  <button
                    className="stop-job-button"
                    type="button"
                    disabled={cancellingJobId === job?.id}
                    onClick={() => job && void cancelJob(job)}
                  >
                    {cancellingJobId === job?.id ? '正在终止…' : '终止任务'}
                  </button>
                )}
              </div>
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
                    <span>表单入口候选</span>
                    <select
                      aria-label="表单入口候选"
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
                    <small>只能选择本次视觉观察中已定位的链接或按钮。</small>
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
                {job.intervention.kind === 'data_required' && (
                  <div className="missing-data-fields">
                    {(job.intervention.missing_fields ?? []).map((field) => (
                      <label key={field.key}>
                        <span>{field.display_name}</span>
                        <input
                          aria-label={field.display_name}
                          type={field.sensitive ? 'password' : field.input_kind === 'select' ? 'text' : field.input_kind}
                          value={missingFieldValues[field.key] ?? ''}
                          onChange={(event) => setMissingFieldValues((current) => ({
                            ...current,
                            [field.key]: event.target.value,
                          }))}
                          autoComplete="off"
                          required
                        />
                        <small>{field.reason}</small>
                      </label>
                    ))}
                  </div>
                )}
                <div className="human-actions">
                  <button className="primary-button human-confirm-button" type="submit" disabled={running}>
                    {running
                      ? '正在继续执行…'
                      : (job.intervention.kind === 'data_required'
                          ? '补齐资料并继续'
                          : (job.intervention.kind === 'submission_confirmation'
                          ? '确认并提交一次'
                          : (job.intervention.kind === 'entry_action_confirmation'
                              ? '确认并进入登录页'
                              : (job.intervention.kind === 'manual_login'
                                  ? '我已完成登录，继续执行'
                                  : '处理完成，继续视觉识别'))))}
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
            {job && <div className={`result-banner ${job.status}`}>
              <strong>{job.message}</strong>
              <span>{job.completed_fields}/{job.total_fields} 字段已验证</span>
              {job.statistics && ['completed', 'failed', 'cancelled'].includes(job.status) && (
                <div className="execution-statistics" aria-label="执行统计">
                  <span>{(job.statistics.duration_ms / 1000).toFixed(2)} 秒</span>
                  <span>{job.statistics.screenshot_count} 次截图</span>
                  <span>{job.statistics.model_call_count} 次模型调用</span>
                  <span>{job.statistics.browser_action_count} 次浏览器动作</span>
                </div>
              )}
              {job.diagnostic_url && <a href={job.diagnostic_url} target="_blank" rel="noreferrer">下载诊断日志</a>}
              {job.statistics_url && <a href={job.statistics_url} target="_blank" rel="noreferrer">下载统计日志</a>}
              {job.download_urls?.map((url, index) => (
                <a key={url} href={url} download>下载办事文档 {index + 1}</a>
              ))}
              {job.browser_session_open && (
                <button
                  className="close-browser-button"
                  type="button"
                  disabled={closingBrowser}
                  onClick={closeCompletedBrowser}
                >
                  {closingBrowser ? '正在关闭…' : '关闭目标浏览器'}
                </button>
              )}
            </div>}
          </section>
        </div>
      </main>)}
    </div>
  )
}
