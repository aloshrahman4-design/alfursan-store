export type Task = { id: string; text: string; isDone: boolean }
export type InboxMessage = { from: string; text: string; at: number }
export type ModelTier = { model: 'haiku' | 'sonnet' | 'opus'; reason: string }
export type Lesson = { at: number; kind: 'correction' | 'manual'; text: string }
export type Tab = 'tasks' | 'messages' | 'model' | 'lessons'

declare module 'claude-code' {
  interface PluginState {
    'alfursan-mods': {
      tasks: Shaped<Task[]>
      inbox: Shaped<InboxMessage[]>
      modelTier: ModelTier | null
      isSaverOn: boolean
      tab: Tab
      lessons: Shaped<Lesson[]>
    }
  }
}
