<template>
  <div class="container my-page-body">
    <div v-if="token && isAdmin" class="p-3">
      <div v-if="waiting">
        <span class="icon is-medium is-size-4">
          <i class="fas fa-spinner fa-pulse"></i>
        </span>
      </div>

      <div v-if="error" class="notification is-danger is-light">
        <button class="delete" @click="error=''"></button>
        {{error}}
      </div>

      <div v-if="devBox">
        <button class="button" @click="getDevBoxStatus">getDevBoxStatus</button>
        <button class="button" @click="setupDevBox">setupDevBox</button>
      </div>
      <div v-else>
        No dev box yet.
      </div>
    </div>
  </div>
</template>

<script>

export default {
  name: 'DevBox',
  data () {
    return {
      waiting: false,
      error: null,
      devBox: null,
    }
  },
  computed: {
    token () {
      return this.$store.state.user.token
    },
    server () {
      return this.$store.state.config.server
    },
    isAdmin () {
      return this.$store.state.user.isAdmin
    },
    adminPrefix () {
      return this.$store.state.config.adminPrefix
    },
  },
  methods: {
    getDevBoxInfo () {
      this.waiting = true
      this.$http.get(this.server + this.adminPrefix + 'get-dev-box-info').then(resp => {
        this.devBox = resp.body
        this.waiting = false
      }, err => {
        this.error = err.body
        this.waiting = false
      })
    },
    getDevBoxStatus () {
      this.$http.get(this.server + this.adminPrefix + 'get-dev-box-status').then(resp => {
        console.log('resp.body', resp.body)
      }, err => {
        this.error = err.body
      })
    },
    setupDevBox () {
      this.$http.post(this.server + this.adminPrefix + 'setup-dev-box', {}).then(resp => {
        console.log('resp.body', resp.body)
      }, err => {
        this.error = err.body
      })
    },
  },
  mounted () {
    this.$nextTick(() => {
      this.getDevBoxInfo()
    })
  },
}
</script>
