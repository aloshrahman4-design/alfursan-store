import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { InboxMessage, Tab, Task, ModelTier } from '../types'

// ثلاث أفكار من فيديو "Claude Code Mods":
// 1) جلسات تتكلم وية بعض: /team يشغّل فريق (باحث، مسوّق، مدير) يتراسلون،
//    و/send يرسل رسالة لجلسة أو وكيل، وكل رسالة توصل تنحفظ بلوحة التحكم.
// 2) توفير الاستهلاك: كل طلب يختارله الموديل المناسب (Haiku للبسيط، Sonnet
//    لليومي، Opus للصعب)، والوكلاء الفرعيين اللي شغلهم بحث/قراءة على Haiku.
// 3) شكل جديد لـ Claude: لوحة تحكم جنب المحادثة بيها المهام والرسائل والموديل.

const PANE = 'alfursan-dashboard'

const tasks = atom({ plugin: 'alfursan-mods', key: 'tasks' } as const, [], { shape: 'tasks-1' })
const inbox = atom({ plugin: 'alfursan-mods', key: 'inbox' } as const, [], { shape: 'inbox-1' })
const tier = atom({ plugin: 'alfursan-mods', key: 'modelTier' } as const, null as ModelTier | null)
const isSaverOn = atom({ plugin: 'alfursan-mods', key: 'isSaverOn' } as const, true)
const tab = atom({ plugin: 'alfursan-mods', key: 'tab' } as const, 'tasks')

// ---------- اختيار الموديل ----------
// تصنيف بالكلمات، بدون أي طلب إضافي للموديل، حتى التوفير نفسه ما يكلف شي.
const HARD = [
  'أمن', 'ثغر', 'اختراق', 'security', 'vulnerab', 'refactor', 'architect', 'صمم', 'تصميم',
  'debug', 'bug', 'خلل', 'مشكلة', 'ليش', 'why', 'migrat', 'deploy', 'نشر', 'تسعير', 'pricing',
  'خطة', 'plan', 'حلل', 'analy', 'performance', 'أداء',
]
const SIMPLE = [
  'شنو', 'what is', 'rename', 'typo', 'ترجم', 'translate', 'format', 'لون', 'color', 'صحح',
  'غير النص', 'اسم', 'list', 'اعرض', 'show', 'شكرا', 'thanks', 'تمام', 'ok',
]

function classify(text: string): ModelTier {
  const t = text.toLowerCase()
  if (t.length > 700 || HARD.some(k => t.includes(k))) {
    return { model: 'opus', reason: 'مهمة صعبة (تحليل/أمن/خلل/تصميم)' }
  }
  if (t.length < 90 && SIMPLE.some(k => t.includes(k))) {
    return { model: 'haiku', reason: 'مهمة بسيطة وقصيرة' }
  }
  return { model: 'sonnet', reason: 'شغل يومي عادي' }
}

// وكلاء شغلهم قراءة وبحث بس: Haiku يكفيهم.
const LIGHT_AGENTS = new Set(['Explore', 'claude-code-guide', 'statusline-setup', 'web-fetch'])

const MODEL_LABEL: Record<ModelTier['model'], string> = {
  haiku: 'Haiku (الأوفر)',
  sonnet: 'Sonnet (اليومي)',
  opus: 'Opus (الأقوى)',
}

// ---------- فريق الجلسات ----------
function teamPrompt(goal: string): string {
  return [
    `شغّل فريق من ثلاث وكلاء بالخلفية لمتجر الفرسان، الهدف: ${goal}`,
    '',
    'استخدم أداة Agent ثلاث مرات مع name مختلف ونفس الهدف:',
    '- name: "researcher" (الباحث، subagent_type: Explore): يجمع المعلومات اللازمة من الكود والملفات.',
    '- name: "marketer" (المسوّق، general-purpose): يكتب النصوص/المنشورات/رسائل الواتساب بالعراقي، بأسلوب موجّه للنساء.',
    '- name: "manager" (المدير، general-purpose): يتابع الاثنين ويرتب الأولويات ويسلمني ملخص نهائي.',
    '',
    'خلّي كل وكيل يرسل نتائجه للثاني بأداة SendMessage (الباحث ← المسوّق ← المدير)،',
    'وبالأخير اعرض لي ملخص المدير بنقاط قصيرة. لا ترسل أي شي لخارج الجلسة بدون موافقتي.',
  ].join('\n')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const stored = await $.store.get('tasks')
    if (Array.isArray(stored)) await update($, tasks, () => stored as Task[])
    const saver = await $.store.get('isSaverOn')
    if (typeof saver === 'boolean') await update($, isSaverOn, () => saver)

    await $.command.register({ name: 'alfursan', description: 'افتح لوحة التحكم: المهام والرسائل والموديل' })
    await $.command.register({ name: 'task', description: 'مهمة جديدة: /task نص | /task done رقم | /task clear' })
    await $.command.register({ name: 'team', description: 'شغّل فريق (باحث، مسوّق، مدير) يتراسلون على هدف: /team الهدف' })
    await $.command.register({ name: 'send', description: 'أرسل رسالة لجلسة أو وكيل: /send الاسم الرسالة' })
    await $.command.register({ name: 'saver', description: 'اختيار الموديل التلقائي: /saver on | off' })

    void $.ui.open({ id: PANE, title: 'لوحة الفرسان' })
    return next(e)
  })

  // ---------- الأوامر ----------
  on('command.run', { command: 'alfursan' }, async $ => {
    await $.ui.open({ id: PANE, title: 'لوحة الفرسان' }).catch(() => undefined)
    return { text: 'انفتحت لوحة التحكم.' }
  })

  on('command.run', { command: 'task' }, async ($, e) => {
    const args = e.args.trim()
    const list = await read($, tasks)
    let next: Task[] = list
    let msg: string

    const done = args.match(/^(done|تم)\s+(\d+)$/i)
    if (!args) {
      msg = list.length
        ? list.map((t, i) => `${i + 1}. ${t.isDone ? '✓' : '○'} ${t.text}`).join('\n')
        : 'ماكو مهام. أضف وحدة: /task نص المهمة'
    } else if (done) {
      const i = Number(done[2]) - 1
      const target = list[i]
      if (!target) return { text: `ماكو مهمة رقم ${i + 1}` }
      next = list.map((t, j) => (j === i ? { ...t, isDone: !t.isDone } : t))
      msg = `${target.isDone ? 'رجعت' : 'خلصت'}: ${target.text}`
    } else if (/^(clear|مسح)$/i.test(args)) {
      next = list.filter(t => !t.isDone)
      msg = `انمسحت ${list.length - next.length} مهام خالصة.`
    } else {
      next = [...list, { id: String(await $.clock.now()), text: args.slice(0, 200), isDone: false }]
      msg = `انضافت: ${args}`
    }

    await update($, tasks, () => next)
    await $.store.set('tasks', next)
    await update($, tab, () => 'tasks' as Tab)
    return { text: msg }
  })

  on('command.run', { command: 'team' }, async ($, e) => {
    const goal = e.args.trim()
    if (!goal) return { text: 'اكتب الهدف، مثلاً: /team جهزلي حملة لمنتجات الشتاء الجديدة' }
    void $.prompt.submit({ text: teamPrompt(goal) })
    await update($, tab, () => 'messages' as Tab)
    return { text: `انطلق الفريق على: ${goal}` }
  })

  on('command.run', { command: 'send' }, async ($, e) => {
    const m = e.args.trim().match(/^(\S+)\s+([\s\S]+)$/)
    const to = m?.[1]
    const body = m?.[2]
    if (!to || !body) return { text: 'الاستخدام: /send الاسم الرسالة' }
    const res = await $.session.send({ to, text: body })
    return { text: res.isDelivered ? `وصلت الرسالة لـ ${to}` : `ما وصلت: ${res.reason ?? 'سبب غير معروف'}` }
  })

  on('command.run', { command: 'saver' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    const isOn = arg === 'on' || arg === 'شغل' ? true : arg === 'off' || arg === 'طفي' ? false : !(await read($, isSaverOn))
    await update($, isSaverOn, () => isOn)
    await $.store.set('isSaverOn', isOn)
    if (!isOn) {
      await update($, tier, () => null)
      $.ui.status(undefined)
    }
    return { text: isOn ? 'اختيار الموديل التلقائي: شغّال' : 'اختيار الموديل التلقائي: طافي (يرجع لموديلك)' }
  })

  // ---------- الرسائل الواصلة ----------
  on('session.receive', async ($, e, next) => {
    if (e.origin.kind === 'peer' || e.origin.kind === 'coordinator' || e.origin.kind === 'peer-send-message') {
      try {
        const from = 'plugin' in e.origin && e.origin.plugin ? e.origin.plugin : e.agentId ?? e.origin.kind
        const item: InboxMessage = { from, text: e.text.slice(0, 500), at: await $.clock.now() }
        await update($, inbox, list => [...list, item].slice(-50))
      } catch {
        // تسجيل الرسالة باللوحة إضافة بس؛ لو فشل، الرسالة نفسها لازم توصل.
      }
    }
    return next(e)
  })

  // ---------- توفير الاستهلاك ----------
  const turnTier = new Map<string, ModelTier>()

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined && e.text && (await read($, isSaverOn))) {
      const t = classify(e.text)
      turnTier.set(e.turnId, t)
      await update($, tier, () => t)
      $.ui.status(`الموديل: ${MODEL_LABEL[t.model]}`)
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    const t = e.agentId === undefined ? turnTier.get(e.turnId) : undefined
    return yield* next(t ? { ...e, model: t.model } : e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) turnTier.delete(e.turnId)
    return next(e)
  })

  on('agent.spawn', async ($, e, next) => {
    const isOn = await read($, isSaverOn).catch(() => false)
    if (e.model === undefined && LIGHT_AGENTS.has(e.subagentType) && isOn) {
      return next({ ...e, model: 'haiku' })
    }
    return next(e)
  })

  // ---------- لوحة التحكم ----------
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const current = await read($, tab)
    const list = await read($, tasks)
    const msgs = await read($, inbox)
    const t = await read($, tier)
    const saver = await read($, isSaverOn)
    const open = list.filter(x => !x.isDone).length

    const setTab = (v: Tab) => () => update($, tab, () => v)
    const toggle = (id: string) => async () => {
      const nextList = (await read($, tasks)).map(x => (x.id === id ? { ...x, isDone: !x.isDone } : x))
      await update($, tasks, () => nextList)
      await $.store.set('tasks', nextList)
    }

    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="row" gap={1}>
          <Button key="t" label={`المهام (${open})`} onPress={setTab('tasks')} />
          <Button key="m" label={`الرسائل (${msgs.length})`} onPress={setTab('messages')} />
          <Button key="d" label="الموديل" onPress={setTab('model')} />
        </Box>

        {current === 'tasks' && (
          <Box flexDirection="column">
            {list.length === 0 && <Text dimColor>ماكو مهام. اكتب: /task نص المهمة</Text>}
            {list.map((x, i) => (
              <Box key={x.id} flexDirection="row" gap={1}>
                <Button key={`b${x.id}`} label={x.isDone ? '✓' : '○'} onPress={toggle(x.id)} />
                <Text dimColor={x.isDone} strikethrough={x.isDone}>
                  {i + 1}. {x.text}
                </Text>
              </Box>
            ))}
          </Box>
        )}

        {current === 'messages' && (
          <Box flexDirection="column">
            {msgs.length === 0 && <Text dimColor>ماكو رسائل لحد الآن. جرّب: /team الهدف</Text>}
            {msgs.slice(-15).map(m => (
              <Box key={`${m.at}-${m.from}`} flexDirection="column">
                <Text bold>{m.from}</Text>
                <Text>{m.text}</Text>
              </Box>
            ))}
          </Box>
        )}

        {current === 'model' && (
          <Box flexDirection="column">
            <Text>الاختيار التلقائي: {saver ? 'شغّال' : 'طافي'} (/saver للتبديل)</Text>
            <Text>
              آخر طلب: {t ? `${MODEL_LABEL[t.model]}، ${t.reason}` : 'ماكو بعد'}
            </Text>
            <Text dimColor>الوكلاء اللي يبحثون ويقرون بس يشتغلون على Haiku.</Text>
          </Box>
        )}
      </Box>
    )
  })
}
