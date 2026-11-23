<script setup lang="ts">
import { Icon } from '@iconify/vue'
import { DataTooltip } from '@/components/ui/data-tooltip'
import { useNodeMainlandBlockStatus } from '@/composables/useNodeMainlandBlockStatus'

const props = defineProps<{
  uuid: string
  name: string
  compact?: boolean
}>()

const { display } = useNodeMainlandBlockStatus(() => props.uuid, () => props.name)
</script>

<template>
  <DataTooltip v-if="display" placement="top" :content="display.tooltip" class="inline-flex shrink-0">
    <span
      class="inline-flex items-center gap-0.5 rounded-full leading-tight font-medium whitespace-nowrap"
      :class="[
        display.badgeClass,
        props.compact ? 'px-1.5 py-0 text-[10px]' : 'px-2 py-0.5 text-[11px]',
      ]"
    >
      <Icon :icon="display.icon" :width="props.compact ? 11 : 12" :height="props.compact ? 11 : 12" class="shrink-0" />
      <span>{{ display.label }}</span>
    </span>
  </DataTooltip>
</template>
