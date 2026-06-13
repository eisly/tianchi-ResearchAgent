<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

type RunState = 'idle' | 'running' | 'done' | 'error' | 'stopped'

type LogEntry = {
  id: number
  level: string
  stage: string
  logger: string
  message: string
  markdown: string
  formatted: string
  time?: string
}

type SseEvent = {
  event: string
  data: string
}

const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

let logId = 0
const question = ref('')
const answer = ref('')
const errorMessage = ref('')
const status = ref<RunState>('idle')
const logs = ref<LogEntry[]>([])
const logPanel = ref<HTMLElement | null>(null)
const abortController = ref<AbortController | null>(null)
const isDark = ref(false)
const logsExpanded = ref(true)

const isRunning = computed(() => status.value === 'running')
const canSend = computed(() => question.value.trim().length > 0 && !isRunning.value)
const statusText = computed(() => {
  if (status.value === 'running') return '运行中'
  if (status.value === 'done') return '已完成'
  if (status.value === 'error') return '出错'
  if (status.value === 'stopped') return '已停止'
  return '待提问'
})

const renderedAnswer = computed(() => renderMarkdown(answer.value))
const latestLog = computed(() => (logs.value.length ? logs.value[logs.value.length - 1] : undefined))

function renderMarkdown(content: string) {
  return DOMPurify.sanitize(markdown.render(content || ''))
}

function clearAll() {
  if (isRunning.value) return
  answer.value = ''
  errorMessage.value = ''
  logs.value = []
  status.value = 'idle'
}

function stopRun() {
  abortController.value?.abort()
  abortController.value = null
  if (isRunning.value) {
    status.value = 'stopped'
    pushLocalLog('system', 'WARN', '用户已停止当前请求。')
  }
}

function toggleTheme() {
  isDark.value = !isDark.value
}

function handleLogToggle(event: Event) {
  logsExpanded.value = (event.target as HTMLDetailsElement).open
}

function pushLocalLog(stage: string, level: string, message: string) {
  logs.value.push({
    id: ++logId,
    level,
    stage,
    logger: 'frontend',
    message,
    markdown: message,
    formatted: message,
    time: new Date().toISOString(),
  })
}

async function sendQuestion() {
  const text = question.value.trim()
  if (!text || isRunning.value) return

  status.value = 'running'
  answer.value = ''
  errorMessage.value = ''
  logs.value = []
  abortController.value = new AbortController()
  pushLocalLog('system', 'INFO', `已提交问题：${text}`)

  try {
    const response = await fetch('/process', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({ question: text }),
      signal: abortController.value.signal,
    })

    if (!response.ok) {
      const detail = await response.text()
      throw new Error(detail || `HTTP ${response.status}`)
    }

    if (!response.body) {
      throw new Error('浏览器未收到可读取的流式响应。')
    }

    await readSseStream(response.body)
    if (status.value === 'running') {
      status.value = 'done'
    }
  } catch (error) {
    if ((error as Error).name === 'AbortError') return
    status.value = 'error'
    errorMessage.value = (error as Error).message || '请求失败'
    pushLocalLog('error', 'ERROR', errorMessage.value)
  } finally {
    abortController.value = null
  }
}

async function readSseStream(stream: ReadableStream<Uint8Array>) {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split(/\r?\n\r?\n/)
    buffer = parts.pop() ?? ''

    for (const part of parts) {
      const event = parseSseBlock(part)
      if (event) handleSseEvent(event)
    }
  }

  buffer += decoder.decode()
  const trailing = parseSseBlock(buffer)
  if (trailing) handleSseEvent(trailing)
}

function parseSseBlock(block: string): SseEvent | null {
  const lines = block.split(/\r?\n/)
  let event = 'message'
  const data: string[] = []

  for (const line of lines) {
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
      continue
    }
    if (line.startsWith('data:')) {
      data.push(line.slice(5).trimStart())
    }
  }

  if (!event && data.length === 0) return null
  return { event, data: data.join('\n') }
}

function handleSseEvent(sse: SseEvent) {
  const eventName = sse.event.toLowerCase()
  if (eventName === 'ping') return

  const payload = parsePayload(sse.data)

  if (eventName === 'log') {
    const stage = String(payload.stage || inferStage(payload.message || payload.logger || 'system'))
    logs.value.push({
      id: ++logId,
      level: String(payload.level || 'INFO'),
      stage,
      logger: String(payload.logger || ''),
      message: String(payload.message || ''),
      markdown: String(payload.markdown || payload.message || ''),
      formatted: String(payload.formatted || payload.message || ''),
      time: payload.time ? String(payload.time) : undefined,
    })
    return
  }

  if (eventName === 'answer' || eventName === 'message') {
    if (typeof payload.answer === 'string') {
      answer.value = payload.answer
    }
    return
  }

  if (eventName === 'error') {
    status.value = 'error'
    errorMessage.value = String(payload.error || payload.answer || '后端执行出错')
    logs.value.push({
      id: ++logId,
      level: 'ERROR',
      stage: 'error',
      logger: 'backend',
      message: errorMessage.value,
      markdown: errorMessage.value,
      formatted: errorMessage.value,
    })
    return
  }

  if (eventName === 'done' && status.value === 'running') {
    status.value = payload.ok === false ? 'error' : 'done'
  }
}

function parsePayload(data: string): Record<string, unknown> {
  if (!data) return {}
  try {
    const parsed = JSON.parse(data)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return { message: data }
  }
}

function inferStage(value: unknown) {
  const text = String(value).toLowerCase()
  if (text.includes('planner')) return 'planner'
  if (text.includes('researcher') || text.includes('research_team')) return 'researcher'
  if (text.includes('search') || text.includes('crawl') || text.includes('tool')) return 'search'
  if (text.includes('reporter') || text.includes('chatbot') || text.includes('[ai]')) return 'final'
  if (text.includes('error') || text.includes('failed')) return 'error'
  return 'system'
}

function stageLabel(stage: string) {
  const map: Record<string, string> = {
    planner: 'Planner',
    researcher: 'Researcher',
    search: 'Search',
    final: 'Final',
    error: 'Error',
    system: 'System',
  }
  return map[stage] || stage
}

function formatTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

watch(
  () => logs.value.length,
  async () => {
    await nextTick()
    if (logsExpanded.value && logPanel.value) {
      logPanel.value.scrollTop = logPanel.value.scrollHeight
    }
  },
)

watch(
  isDark,
  (value) => {
    document.documentElement.dataset.theme = value ? 'dark' : 'light'
  },
  { immediate: true },
)
</script>

<template>
  <main class="app-shell" :class="{ 'theme-dark': isDark }">
    <section class="topbar">
      <div>
        <p class="eyebrow">ResearchAgent</p>
        <h1>AI Research Dashboard</h1>
      </div>
      <div class="topbar-tools">
        <button class="theme-toggle" type="button" @click="toggleTheme">
          {{ isDark ? '浅色模式' : '深色模式' }}
        </button>
        <div class="status-pill" :class="`status-${status}`">
          <span class="status-dot"></span>
          {{ statusText }}
        </div>
      </div>
    </section>

    <section class="question-panel">
      <label class="input-label" for="question">问题</label>
      <textarea
        id="question"
        v-model="question"
        :disabled="isRunning"
        placeholder="输入你想交给 ResearchAgent 调研的问题..."
        rows="4"
        @keydown.ctrl.enter.prevent="sendQuestion"
      ></textarea>
      <div class="actions">
        <button class="primary" :disabled="!canSend" @click="sendQuestion">发送</button>
        <button :disabled="!isRunning" @click="stopRun">停止</button>
        <button :disabled="isRunning" @click="clearAll">清空</button>
      </div>
    </section>

    <section class="dashboard-grid">
      <article class="panel log-section">
        <details class="log-dropdown" :open="logsExpanded" @toggle="handleLogToggle">
          <summary class="panel-header log-summary">
            <div>
              <p class="eyebrow">Trace</p>
              <h2>运行日志</h2>
              <p v-if="latestLog && !logsExpanded" class="latest-log">
                {{ latestLog.formatted }}
              </p>
            </div>
            <div class="summary-actions">
              <span class="counter">{{ logs.length }} 条</span>
              <span class="chevron">{{ logsExpanded ? '收起' : '展开' }}</span>
            </div>
          </summary>

          <div ref="logPanel" class="log-list" aria-live="polite">
            <div v-if="logs.length === 0" class="empty-state">
              日志会在 Agent 开始执行后实时显示。
            </div>
            <div
              v-for="log in logs"
              :key="log.id"
              class="log-entry"
              :class="[`stage-${log.stage}`, { 'is-error': log.level === 'ERROR' }]"
            >
              <div class="log-meta">
                <span class="stage-tag">{{ stageLabel(log.stage) }}</span>
                <span class="level-tag">{{ log.level }}</span>
                <span class="logger-name">{{ log.logger }}</span>
                <time>{{ formatTime(log.time) }}</time>
              </div>
              <p class="formatted-log">{{ log.formatted }}</p>
              <div class="markdown-body log-content" v-html="renderMarkdown(log.markdown)"></div>
            </div>
          </div>
        </details>
      </article>

      <article class="panel answer-section">
        <div class="panel-header">
          <div>
            <p class="eyebrow">Result</p>
            <h2>最终答案</h2>
          </div>
        </div>

        <div v-if="errorMessage" class="error-box">
          {{ errorMessage }}
        </div>
        <div v-else-if="answer" class="markdown-body answer-body" v-html="renderedAnswer"></div>
        <div v-else class="empty-state answer-empty">
          完成后会在这里展示 answer。
        </div>
      </article>
    </section>
  </main>
</template>

<style scoped>
:global(*) {
  box-sizing: border-box;
}

:global(body) {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
  color: #18212f;
  background:
    radial-gradient(circle at top left, rgba(34, 128, 141, 0.16), transparent 28rem),
    linear-gradient(135deg, #f7f4ee 0%, #eef5f3 45%, #f8fafc 100%);
  font-family:
    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

:global(html[data-theme='dark'] body) {
  color: #e5edf7;
  background:
    radial-gradient(circle at top left, rgba(20, 184, 166, 0.16), transparent 30rem),
    linear-gradient(135deg, #0b1120 0%, #111827 50%, #172033 100%);
}

button,
textarea {
  font: inherit;
}

.app-shell {
  width: min(1440px, 100%);
  min-height: 100vh;
  margin: 0 auto;
  padding: 28px;
}

.topbar,
.question-panel,
.panel {
  border: 1px solid rgba(24, 33, 47, 0.1);
  background: rgba(255, 255, 255, 0.78);
  box-shadow: 0 18px 42px rgba(40, 48, 61, 0.08);
  backdrop-filter: blur(16px);
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  padding: 20px 22px;
  border-radius: 8px;
}

.topbar-tools {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px;
}

.eyebrow {
  margin: 0 0 6px;
  color: #6b7280;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}

h1,
h2 {
  margin: 0;
  letter-spacing: 0;
}

h1 {
  font-size: clamp(28px, 4vw, 44px);
  line-height: 1.04;
}

h2 {
  font-size: 20px;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  min-height: 34px;
  padding: 7px 12px;
  border: 1px solid #d4dbe4;
  border-radius: 999px;
  background: #ffffff;
  color: #465365;
  font-size: 14px;
  font-weight: 700;
}

.status-dot {
  width: 9px;
  height: 9px;
  border-radius: 999px;
  background: #8894a4;
}

.status-running .status-dot {
  background: #0f9f6e;
  box-shadow: 0 0 0 6px rgba(15, 159, 110, 0.12);
}

.status-done .status-dot {
  background: #2563eb;
}

.status-error .status-dot {
  background: #dc2626;
}

.status-stopped .status-dot {
  background: #c2410c;
}

.question-panel {
  margin-bottom: 16px;
  padding: 18px;
  border-radius: 8px;
}

.input-label {
  display: block;
  margin-bottom: 8px;
  color: #334155;
  font-size: 14px;
  font-weight: 800;
}

textarea {
  width: 100%;
  min-height: 116px;
  resize: vertical;
  padding: 14px 15px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  outline: none;
  background: #ffffff;
  color: #111827;
  line-height: 1.6;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease;
}

textarea:focus {
  border-color: #22808d;
  box-shadow: 0 0 0 4px rgba(34, 128, 141, 0.12);
}

textarea:disabled {
  color: #6b7280;
  background: #f8fafc;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
}

button {
  min-width: 88px;
  min-height: 40px;
  padding: 9px 14px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #ffffff;
  color: #1f2937;
  cursor: pointer;
  font-weight: 800;
  transition:
    transform 160ms ease,
    border-color 160ms ease,
    background 160ms ease;
}

button:hover:not(:disabled) {
  transform: translateY(-1px);
  border-color: #94a3b8;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.52;
}

button.primary {
  border-color: #1d6f7a;
  background: #1d6f7a;
  color: #ffffff;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.panel {
  min-width: 0;
  width: 100%;
  border-radius: 8px;
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 16px 18px;
  border-bottom: 1px solid rgba(24, 33, 47, 0.08);
  background: rgba(248, 250, 252, 0.82);
}

.counter {
  flex: 0 0 auto;
  color: #64748b;
  font-size: 13px;
  font-weight: 800;
}

.log-dropdown {
  width: 100%;
}

.log-dropdown summary {
  list-style: none;
  cursor: pointer;
}

.log-dropdown summary::-webkit-details-marker {
  display: none;
}

.log-summary {
  user-select: none;
}

.summary-actions {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 10px;
}

.chevron {
  min-width: 54px;
  min-height: 30px;
  padding: 5px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 999px;
  color: #334155;
  background: #ffffff;
  font-size: 13px;
  font-weight: 800;
  text-align: center;
}

.latest-log {
  max-width: min(920px, 72vw);
  margin: 8px 0 0;
  overflow: hidden;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.log-list {
  height: min(62vh, 680px);
  min-height: 420px;
  overflow-y: auto;
  padding: 14px;
  scroll-behavior: smooth;
}

.log-entry {
  margin-bottom: 12px;
  padding: 13px 14px;
  border: 1px solid #dbe3ec;
  border-left: 4px solid #94a3b8;
  border-radius: 8px;
  background: #ffffff;
}

.stage-planner {
  border-left-color: #7c3aed;
}

.stage-researcher {
  border-left-color: #0f9f6e;
}

.stage-search {
  border-left-color: #d97706;
}

.stage-final {
  border-left-color: #2563eb;
}

.stage-error,
.is-error {
  border-left-color: #dc2626;
  background: #fff7f7;
}

.log-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
  margin-bottom: 9px;
  color: #64748b;
  font-size: 12px;
}

.stage-tag,
.level-tag {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 2px 8px;
  border-radius: 999px;
  font-weight: 800;
}

.stage-tag {
  background: #eaf4f4;
  color: #17626b;
}

.level-tag {
  background: #eef2f7;
  color: #475569;
}

.logger-name {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.formatted-log {
  max-height: 92px;
  margin: 0 0 10px;
  overflow: auto;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
}

.answer-body,
.answer-empty,
.error-box {
  height: min(62vh, 680px);
  min-height: 420px;
  overflow: auto;
  padding: 18px;
}

.answer-section {
  position: static;
}

.empty-state {
  display: grid;
  min-height: 180px;
  place-items: center;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  color: #64748b;
  background: rgba(255, 255, 255, 0.62);
  text-align: center;
}

.error-box {
  color: #991b1b;
  background: #fff7f7;
  line-height: 1.7;
}

.markdown-body {
  color: #1f2937;
  font-size: 14px;
  line-height: 1.72;
  overflow-wrap: anywhere;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: 0.8em 0 0.45em;
  line-height: 1.25;
}

.markdown-body :deep(h1) {
  font-size: 24px;
}

.markdown-body :deep(h2) {
  font-size: 20px;
}

.markdown-body :deep(h3) {
  font-size: 17px;
}

.markdown-body :deep(p) {
  margin: 0.55em 0;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 1.35rem;
}

.markdown-body :deep(a) {
  color: #1d6f7a;
  font-weight: 700;
}

.markdown-body :deep(code) {
  padding: 2px 5px;
  border-radius: 5px;
  background: #eef2f7;
  font-size: 0.92em;
}

.markdown-body :deep(pre) {
  max-width: 100%;
  overflow-x: auto;
  padding: 12px;
  border-radius: 8px;
  background: #111827;
  color: #e5e7eb;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background: transparent;
  color: inherit;
}

.log-content {
  max-height: none;
  overflow: auto;
}

.theme-dark .topbar,
.theme-dark .question-panel,
.theme-dark .panel {
  border-color: rgba(148, 163, 184, 0.2);
  background: rgba(15, 23, 42, 0.84);
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.28);
}

.theme-dark .panel-header {
  border-bottom-color: rgba(148, 163, 184, 0.18);
  background: rgba(17, 24, 39, 0.88);
}

.theme-dark h1,
.theme-dark h2,
.theme-dark .input-label,
.theme-dark .markdown-body {
  color: #e5edf7;
}

.theme-dark .eyebrow,
.theme-dark .counter,
.theme-dark .logger-name,
.theme-dark .latest-log,
.theme-dark .formatted-log,
.theme-dark .log-meta {
  color: #9caec4;
}

.theme-dark textarea,
.theme-dark button,
.theme-dark .status-pill,
.theme-dark .chevron {
  border-color: rgba(148, 163, 184, 0.28);
  background: #111827;
  color: #e5edf7;
}

.theme-dark textarea:disabled {
  background: #0f172a;
  color: #94a3b8;
}

.theme-dark button.primary {
  border-color: #14b8a6;
  background: #0f766e;
  color: #f8fafc;
}

.theme-dark .log-entry {
  border-color: rgba(148, 163, 184, 0.18);
  background: rgba(15, 23, 42, 0.72);
}

.theme-dark .stage-tag {
  background: rgba(20, 184, 166, 0.16);
  color: #5eead4;
}

.theme-dark .level-tag,
.theme-dark .markdown-body :deep(code) {
  background: rgba(148, 163, 184, 0.16);
  color: #cbd5e1;
}

.theme-dark .empty-state {
  border-color: rgba(148, 163, 184, 0.28);
  color: #94a3b8;
  background: rgba(15, 23, 42, 0.42);
}

.theme-dark .error-box,
.theme-dark .stage-error,
.theme-dark .is-error {
  background: rgba(127, 29, 29, 0.24);
  color: #fecaca;
}

.theme-dark .markdown-body :deep(a) {
  color: #5eead4;
}

@media (max-width: 980px) {
  .app-shell {
    padding: 18px;
  }

  .log-list,
  .answer-body,
  .answer-empty,
  .error-box {
    min-height: 360px;
  }
}

@media (max-width: 640px) {
  .app-shell {
    padding: 12px;
  }

  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .topbar-tools {
    justify-content: flex-start;
  }

  h1 {
    font-size: 30px;
  }

  .actions button {
    flex: 1 1 0;
    min-width: 0;
  }

  .log-list {
    height: 58vh;
  }
}
</style>
