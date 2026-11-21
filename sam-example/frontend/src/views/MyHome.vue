<template>
  <div class="container my-page-body">
    <section class="section" v-if="!token">
      <h1 class="title is-5">Welcome</h1>
      <h2 class="subtitle is-6 mt-2">
        Please sign in to access your account.
      </h2>

      <div class="field">
        <label class="label">Email</label>
        <div class="control has-icons-left has-icons-right">
          <input class="input" placeholder="Email" v-model="loginEmail">
          <span class="icon is-small is-left">
            <i class="fas fa-envelope"></i>
          </span>
        </div>
      </div>

      <div class="field">
        <label class="label">Password</label>
        <div class="control has-icons-left has-icons-right">
          <input class="input" type="password" placeholder="Password" v-model="password" @keyup.enter="signin">
          <span class="icon is-small is-left">
            <i class="fas fa-key"></i>
          </span>
        </div>
      </div>

      <div v-if="error" class="notification is-danger is-light mt-4">
        <button class="delete" @click="error=''"></button>
        {{error}}
      </div>

      <div class="field is-grouped mt-5">
        <div class="control">
          <button class="button is-link" :disabled="!formValid" :class="{'is-loading': waiting}" @click="signin">Sign in</button>
        </div>
      </div>
    </section>

    <section class="section" v-if="token">
      <h1 class="title is-5">Home</h1>
      <h2 class="subtitle is-6 mt-2">
        Welcome back, {{ username || 'User' }}!
      </h2>
    </section>
    
  </div>
</template>

<script>
import Vue from 'vue'

export default {
  name: 'my-home',
  components: {
  },
  data () {
    return {
      error: '',
      waiting: false,
      loginEmail: '',
      password: '',
    }
  },
  computed: {
    server () {
      return this.$store.state.config.server
    },
    token () {
      return this.$store.state.user.token
    },
    username () {
      return this.$store.state.user.username
    },
    email () {
      return this.$store.state.user.email
    },
    groups () {
      return this.$store.state.user.groups
    },
    isAdmin () {
      return this.$store.state.user.isAdmin
    },
    emailValid () {
      var re = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/
      return re.test(this.loginEmail.trim().toLowerCase())
    },
    formValid () {
      return this.emailValid && this.password
    },
  },
  methods: {
    signin () {
      if (this.waiting) {
        return
      }
      this.waiting = true
      this.loginEmail = this.loginEmail.trim().toLowerCase()
      var message = {email: this.loginEmail, password: this.password}
      this.$http.post(this.server + '/api/user/auth-user/', message).then(resp => {
        if (resp.body) {
          var user = resp.body
          Vue.http.headers.common['Authorization'] = user.token
          this.$store.commit('user/setUser', user)
          this.loginEmail = ''
          this.password = ''
          this.error = ''
        } else {
          this.error = 'Failed to sign in!'
          this.$store.commit('user/reset')
        }
        this.waiting = false
      }, err => {
        this.error = err.body || 'Failed to sign in!'
        this.waiting = false
      })
    },
  },
  mounted () {
    
  },
  unmounted () {
    
  },
}
</script>

<style scoped>

</style>
