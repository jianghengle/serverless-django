<template>
  <div class="chat-agent">
    <div class="chat-resize-handle" :class="{ dragging: resizing }" @pointerdown.prevent="startResize"></div>
    <div class="chat-messages" ref="messagesContainer">
      <!-- Welcome message -->
      <div v-if="messages.length === 0" class="chat-welcome">
        <p>Hi! I can help you code your web app. Try asking:</p>
        <ul>
          <li @click="submitSuggestion('What tables are there?')">What tables are there?</li>
        </ul>
      </div>

      <!-- Message list -->
      <div v-for="(msg, i) in messages" :key="i" :class="'chat-msg--' + msg.role">
        <div class="chat-bubble">
          <!-- Tool call badges -->
          <div v-if="msg.toolCalls && msg.toolCalls.length" class="chat-tool-badges">
            <span v-for="(tc, j) in msg.toolCalls" :key="j" class="chat-tool-badge">📊 {{ tc.name }}</span>
          </div>
          <div v-if="msg.text" class="chat-text" v-html="renderMarkdown(msg.text)"></div>
        </div>
      </div>

      <!-- Loading indicator -->
      <div v-if="isLoading" class="chat-msg chat-msg--assistant">
        <div class="chat-bubble">
          <div v-if="activeToolCalls.length" class="chat-tool-status">
            <div v-for="(tc, i) in activeToolCalls" :key="i" class="chat-tool-call">
              <span :class="'chat-tool-dot ' + tc.status"></span>
              <span class="chat-tool-name">{{ tc.status === 'calling' ? '⏳' : '✓' }} {{ tc.name }}</span>
            </div>
          </div>
          <p class="chat-typing">Thinking...</p>
        </div>
      </div>
    </div>

    <!-- Uncommitted changes warning -->
    <div v-if="authed && !isLoading && gitStatus.files.length" class="chat-git-warning">
      <div class="chat-git-dropdown">
        <div class="chat-git-menu">
          <ul class="chat-git-files">
            <li v-for="f in gitStatus.files" :key="f.path">
              <span :class="'chat-git-tag chat-git-tag--' + f.status">{{ f.status }}</span>
              <span class="chat-git-path">{{ f.path }}</span>
            </li>
          </ul>
          <div class="chat-git-plan">
            Commit &amp; push<span v-if="gitStatus.backend">, deploy backend</span><span v-if="gitStatus.frontend">, deploy frontend</span>
          </div>
          <button class="chat-send chat-git-deploy" @click="commitAndDeploy">Commit &amp; Deploy</button>
        </div>
        <button class="chat-git-button" type="button">
          ⚠ {{ gitStatus.files.length }} uncommitted change{{ gitStatus.files.length === 1 ? '' : 's' }} ▾
        </button>
      </div>
    </div>

    <!-- Input area -->
    <div class="chat-input-area">
      <textarea
        class="chat-input"
        v-model="inputValue"
        @keydown.enter.exact.prevent="handleSubmit"
        :disabled="isLoading"
        rows="1"
      ></textarea>
      <button class="chat-send" :disabled="!inputValue.trim() || isLoading" @click="handleSubmit">
        <span class="icon is-small">
          <i class="fas fa-paper-plane"></i>
        </span>
      </button>
    </div>
  </div>
</template>

<script>
import axios from 'axios'
import showdown from 'showdown'
import { sanitizeHtml } from './sanitizeHtml.js'

const MIN_WIDTH = 320


export default {
  name: 'coding-agent',
  data () {
    return {
      inputValue: '',
      isLoading: false,
      messages: [],         // { role, text }
      activeToolCalls: [],
      mdConverter: new showdown.Converter({ tables: true }),
      wsClient: null,
      authed: false,
      resizing: false,
      gitStatus: { files: [], backend: false, frontend: false },
    }
  },
  computed: {
    token () {
      return this.$store.state.user.token
    },
  },
  watch: {
    messages () {
      this.$nextTick(() => {
        this.scrollToBottom()
      })
    },
  },
  methods: {
    // ─── RESIZE ────────────────────────────────────────────────────────────────
    startResize (e) {
      this.resizing = true
      this.resizeStartX = e.clientX
      this.resizeStartWidth = this.$el.getBoundingClientRect().width
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'col-resize'
      window.addEventListener('pointermove', this.onResize)
      window.addEventListener('pointerup', this.stopResize)
      window.addEventListener('pointercancel', this.stopResize)
    },
    onResize (e) {
      var max = Math.max(MIN_WIDTH, window.innerWidth - 200)  // always leave room for the page
      var width = this.resizeStartWidth + (e.clientX - this.resizeStartX)
      width = Math.round(Math.min(max, Math.max(MIN_WIDTH, width)))
      this.$store.commit('config/setAiCodingWidth', String(width))
    },
    stopResize () {
      this.resizing = false
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      window.removeEventListener('pointermove', this.onResize)
      window.removeEventListener('pointerup', this.stopResize)
      window.removeEventListener('pointercancel', this.stopResize)
    },

    // ─── UI METHODS ────────────────────────────────────────────────────────────
    scrollToBottom () {
      var container = this.$refs.messagesContainer
      if (container) {
        container.scrollTop = container.scrollHeight
      }
    },

    renderMarkdown (text) {
      if (!text) return ''
      // Strip the json code block for display (structured data handled separately)
      var cleaned = text.replace(/```json\s*\n[\s\S]*?\n```/g, '').trim()
      // Model output is untrusted (prompt injection): sanitize before v-html.
      return sanitizeHtml(this.mdConverter.makeHtml(cleaned))
    },

    submitSuggestion (text) {
      this.inputValue = text
      this.handleSubmit()
    },

    commitAndDeploy () {
      if (this.isLoading || !this.authed || !this.wsClient || this.wsClient.readyState !== WebSocket.OPEN) return
      var parts = ['commit and push']
      if (this.gitStatus.backend) parts.push('deploy the backend')
      if (this.gitStatus.frontend) parts.push('deploy the frontend')
      if (!window.confirm('This will ' + parts.join(', ') + ' (' + this.gitStatus.files.length + ' changed files). Continue?')) return
      this.messages.push({ role: 'user', text: 'Commit & Deploy' })
      this.isLoading = true
      this.activeToolCalls = []
      this.wsClient.send(JSON.stringify({ type: 'commit_deploy' }))
    },

    // ─── SUBMIT HANDLER ────────────────────────────────────────────────────────
    handleSubmit () {
      var text = this.inputValue.trim()
      if (!text || this.isLoading) return

      this.messages.push({ role: 'user', text: text })
      this.inputValue = ''

      if (!this.wsClient || this.wsClient.readyState !== WebSocket.OPEN || !this.authed) {
        this.messages.push({ role: 'assistant', text: 'Not connected to the server.' })
        return
      }
      this.isLoading = true
      this.activeToolCalls = []
      this.wsClient.send(JSON.stringify({ type: 'user_message', text: text }))
    },

    // ─── WEBSOCKET ─────────────────────────────────────────────────────────────
    connect () {
      var url = import.meta.env.VITE_CODING_AGENT_WS_URL || 'ws://localhost:8765'
      var ws = new WebSocket(url)
      this.wsClient = ws

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: 'auth', token: this.token }))
      }
      ws.onmessage = (event) => {
        var data
        try {
          data = JSON.parse(event.data)
        } catch (e) {
          return
        }
        if (data.type === 'auth_ok') {
          this.authed = true
        } else if (data.type === 'git_status') {
          this.gitStatus = { files: data.files || [], backend: !!data.backend, frontend: !!data.frontend }
        } else if (data.type === 'history') {
          // Replace local messages with the server's copy (also covers reconnects).
          this.messages = data.messages || []
          this.activeToolCalls = []
          this.isLoading = !!data.busy
        } else if (data.type === 'auth_failed') {
          this.authed = false
          this.messages.push({ role: 'assistant', text: 'Error: ' + data.message })
        } else if (data.type === 'tool_call') {
          if (data.status === 'calling') {
            this.activeToolCalls.push({ name: data.name, status: 'calling' })
          } else {
            var idx = this.activeToolCalls.length - 1
            while (idx >= 0 && !(this.activeToolCalls[idx].name === data.name && this.activeToolCalls[idx].status === 'calling')) idx--
            if (idx >= 0) this.$set(this.activeToolCalls, idx, { name: data.name, status: 'done' })
          }
        } else if (data.type === 'assistant_message') {
          this.messages.push({ role: 'assistant', text: data.message, toolCalls: this.activeToolCalls.slice() })
          this.activeToolCalls = []
          this.isLoading = false
        } else if (data.type === 'error') {
          this.messages.push({ role: 'assistant', text: 'Error: ' + data.message })
          this.activeToolCalls = []
          this.isLoading = false
        }
      }
      ws.onerror = () => {
        console.error('[CodingAgent] WebSocket error')
      }
      ws.onclose = () => {
        if (this.wsClient === ws) {
          this.wsClient = null
          this.authed = false
          if (this.isLoading) {
            this.isLoading = false
            this.messages.push({ role: 'assistant', text: 'Error: connection to the server was lost.' })
          }
        }
      }
    },
  },
  created () {
    this.connect()
  },
  beforeDestroy () {
    this.stopResize()
    if (this.wsClient) {
      var ws = this.wsClient
      this.wsClient = null
      ws.close()
    }
  },
}
</script>

<style scoped>
.chat-agent {
  position: relative;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
}

.chat-resize-handle {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 6px;
  cursor: col-resize;
  z-index: 10;
  touch-action: none;
}
.chat-resize-handle:hover,
.chat-resize-handle.dragging {
  background: rgba(50, 115, 220, 0.35);
}

.chat-header {
  display: flex;
  align-items: center;
  padding: 14px 20px;
  background: #363636;
  color: white;
}
.chat-title {
  font-weight: 600;
  font-size: 16px;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}

.chat-welcome {
  color: #666;
  font-size: 14px;
  max-width: 600px;
}
.chat-welcome ul {
  margin-top: 8px;
  padding-left: 16px;
}
.chat-welcome li {
  margin: 6px 0;
  cursor: pointer;
  color: #3273dc;
}
.chat-welcome li:hover {
  text-decoration: underline;
}

.chat-msg {
  margin-bottom: 16px;
  display: flex;
}
.chat-msg--user {
  justify-content: flex-end;
}
.chat-msg--assistant {
  justify-content: flex-start;
}

.chat-bubble {
  max-width: 100%;
  width: fit-content;
  padding: 12px 16px;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.5;
  margin-bottom: 15px;
}
.chat-msg--user .chat-bubble {
  background: #3273dc;
  color: white;
}
.chat-msg--assistant .chat-bubble {
  background: #f5f5f5;
  color: #1a1a1a;
}

.chat-text {
  margin: 0;
  word-break: break-word;
  width: fit-content;
  max-width: 100%;
}
.chat-text >>> p {
  margin: 0 0 0.5em 0;
}
.chat-text >>> p:last-child {
  margin-bottom: 0;
}
.chat-text >>> ul,
.chat-text >>> ol {
  margin: 0 0 0.5em 0;
  padding-left: 1.5em;
}
.chat-text >>> ul { list-style: disc; }
.chat-text >>> ol { list-style: decimal; }
.chat-text >>> li { margin: 0.2em 0; }
.chat-text >>> ul ul,
.chat-text >>> ol ol {
  margin-bottom: 0;
}

.chat-tool-badges {
  margin-bottom: 8px;
}
.chat-tool-badge {
  display: inline-block;
  background: #e8f4fd;
  color: #3273dc;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  margin-right: 4px;
}

.chat-tool-status {
  margin-bottom: 6px;
}
.chat-tool-call {
  display: flex;
  align-items: center;
  font-size: 12px;
  color: #666;
  margin: 3px 0;
}
.chat-tool-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 8px;
}
.chat-tool-dot.calling {
  background: #ffdd57;
  animation: pulse 1s infinite;
}
.chat-tool-dot.done {
  background: #48c774;
}
.chat-tool-name {
  font-family: monospace;
  font-size: 11px;
}

.chat-typing {
  color: #999;
  font-size: 12px;
  font-style: italic;
  margin: 0;
}

.chat-table-wrapper {
  overflow-x: auto;
  margin: 8px 0;
}
.chat-table-wrapper table {
  font-size: 13px;
}

.chat-chart-wrapper {
  margin: 8px 0;
  width: 100%;
  min-height: 300px;
}

.chat-git-warning {
  padding: 0 20px;
}
.chat-git-dropdown {
  position: relative;
  display: inline-block;
}
.chat-git-button {
  background: #fffaeb;
  color: #946c00;
  border: 1px solid #ffe08a;
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 13px;
  cursor: default;
}
.chat-git-menu {
  display: none;
  position: absolute;
  bottom: 100%;           /* opens upward; no gap so hover is not lost */
  left: 0;
  width: 300px;
  max-width: calc(100vw - 40px);
  padding: 10px 12px;
  margin-bottom: 0;
  background: #fff;
  border: 1px solid #dbdbdb;
  border-radius: 8px;
  box-shadow: 0 -4px 14px rgba(0, 0, 0, 0.12);
  z-index: 20;
}
.chat-git-dropdown:hover .chat-git-menu,
.chat-git-dropdown:focus-within .chat-git-menu {
  display: block;
}
.chat-git-files {
  list-style: none;
  margin: 0 0 8px 0;
  padding: 0;
  max-height: 220px;
  overflow-y: auto;
  font-size: 12px;
}
.chat-git-files li {
  display: flex;
  align-items: baseline;
  margin: 3px 0;
}
.chat-git-path {
  font-family: monospace;
  word-break: break-all;
}
.chat-git-tag {
  flex: none;
  width: 58px;
  margin-right: 6px;
  font-size: 10px;
  text-transform: uppercase;
  color: #946c00;
}
.chat-git-tag--new { color: #257942; }
.chat-git-tag--deleted { color: #cc0f35; }
.chat-git-plan {
  font-size: 12px;
  color: #666;
  margin-bottom: 8px;
}
.chat-git-deploy {
  width: 100%;
}

.chat-input-area {
  display: flex;
  padding: 16px 20px;
  gap: 10px;
}
.chat-input {
  flex: 1;
  border: 1px solid #dbdbdb;
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 14px;
  resize: none;
  outline: none;
}
.chat-input:focus {
  border-color: #3273dc;
}
.chat-input:disabled {
  background: #f9f9f9;
}
.chat-send {
  background: #3273dc;
  color: white;
  border: none;
  border-radius: 8px;
  padding: 10px 20px;
  font-size: 14px;
  cursor: pointer;
}
.chat-send:hover:not(:disabled) {
  background: #2366d1;
}
.chat-send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
</style>
