<template>
  <div id="app">
    <div class="columns">
      <div class="column is-narrow coding-agent-container" v-if="aiCoding">
        <div style="height:calc(100vh)" :style="{'width': aiCodingWidth + 'px'}">
          <coding-agent />
        </div>
      </div>
      <div class="column">
        <my-header />
        <router-view />
      </div>
    </div>
    <confirm-modal />
  </div>
</template>

<script>
import MyHeader from './views/MyHeader.vue'
import ConfirmModal from './components/modals/ConfirmModal.vue'
import CodingAgent from './components/coding-agent/CodingAgent.vue'

export default {
  components: {
    MyHeader,
    ConfirmModal,
    CodingAgent
  },
  computed: {
    aiCoding () {
      return this.$store.state.config.aiCoding
    },
    aiCodingWidth () {
      return this.$store.state.config.aiCodingWidth
    },
  },
}
</script>

<style>
.button.is-link.my-disabled-button {
  opacity: 0.5;
  background-color: #485fc7;
  border-color: #485fc7;
  box-shadow: none;
}

.my-disbaled-field {
  -webkit-text-fill-color: #7a7a7a;
  opacity: 1; /* required on iOS */
}

.my-page-body {
  min-height: calc(100vh - 300px);
}

.coding-agent-container {
  border-right: 1px solid #f3f4f6;
  padding-right: 0px !important;
}
</style>
