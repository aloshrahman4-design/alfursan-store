import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'alfursan-mods',
  component: 'Pane',
  requestId: 'alfursan-dashboard',
  props: {
    title: 'لوحة الفرسان',
    isFocused: false,
    bodyColumns: 50,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 20 },
    view: {},
  },
} as const

test('a task added with /task shows in the dashboard', async ($, on) => {
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('clock.now', () => ({ value: 1 }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r = await $.command.run({ command: 'task', args: 'صور منتجات الشتاء' })
  expect(r.text).toBe('انضافت: صور منتجات الشتاء')

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const row = await ui.find({ type: 'Text', text: /الشتاء/ })
  expect(row?.text).toBe('1. صور منتجات الشتاء')
  await ui.unmount()
})

test('a read-only subagent is moved to haiku', async ($, on) => {
  on('agent.spawn', (_, e) => ({ model: e.model ?? 'inherit', agentId: 'a1' }))
  const light = await $.agent.spawn({ prompt: 'find files', subagentType: 'Explore' })
  expect(light.model).toBe('haiku')
  const heavy = await $.agent.spawn({ prompt: 'fix the bug', subagentType: 'general-purpose' })
  expect(heavy.model).toBe('inherit')
})

test('a correction is remembered and reaches the system prompt', async ($, on) => {
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('store.get', () => ({ value: undefined }))
  on('store.set', () => ({ value: undefined }))
  on('clock.now', () => ({ value: 1 }))
  on('prompt.submit', (_, e) => ({ text: e.text }))
  on('prompt.compose', () => ({ sections: [] }))

  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'لا، مو هيج، استخدم Decimal مو float' })
  await $.prompt.submit({ text: 'شكرا، سوي الصفحة' })

  const r = await $.command.run({ command: 'lesson', args: '' })
  expect(r.text).toContain('Decimal')
  expect(r.text).not.toContain('الصفحة')

  const composed = await $.prompt.compose({ model: 'sonnet', promptModel: 'sonnet', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] })
  expect(composed.sections.some(s => s.id === 'alfursan-mods:lessons' && s.text.includes('Decimal'))).toBe(true)
})
