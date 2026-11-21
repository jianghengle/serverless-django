const dev = process.env.NODE_ENV == 'development'

// initial state
export const state = {
  server: dev ? 'http://localhost:3000' : '/Prod',
  adminPrefix: '/api/admin/',
  aiCoding: localStorage.getItem('aiCoding') == 'true',
  aiCodingWidth: localStorage.getItem('aiCodingWidth') || '380',
}

// mutations
export const mutations = {
  setAiCoding (state, coding) {
    state.aiCoding = coding
    localStorage.setItem('aiCoding', JSON.stringify(coding))
  },
  toggleAiCoding (state) {
    state.aiCoding = !state.aiCoding
    localStorage.setItem('aiCoding', JSON.stringify(state.aiCoding))
  },
  setAiCodingWidth (state, width) {
    state.aiCodingWidth = width
    localStorage.setItem('aiCodingWidth', width)
  },
}

export default {
  namespaced: true,
  state,
  mutations
}
