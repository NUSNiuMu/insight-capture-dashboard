import { ref, watch } from 'vue'
import english from './locales/en.json'
const storageKey = 'insight-capture.language'
function savedLanguage() { try { return localStorage.getItem(storageKey) === 'en' ? 'en' : 'zh' } catch { return 'zh' } }
export const language = ref(savedLanguage())
watch(language, value => { document.documentElement.lang = value === 'en' ? 'en' : 'zh-CN' }, { immediate: true })
export function setLanguage(value) {
  if (!['zh', 'en'].includes(value)) return
  language.value = value
  try { localStorage.setItem(storageKey, value) } catch { /* Current session can still switch. */ }
}
const slot = /\{(\d+)\}/g
const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const patterns = Object.entries(english).filter(([key]) => /\{\d+\}/.test(key)).map(([key, value]) => ({
  regex: new RegExp('^' + key.split(/\{\d+\}/).map(escape).join('([\\s\\S]*?)') + '$'), value,
}))
function format(value, args) { return value.replace(slot, (match, index) => args[index] == null ? match : String(args[index])) }
// Call only for interface-owned text, never for task names, topics, paths, or Wi-Fi names.
export function t(value, ...args) {
  if (typeof value !== 'string') return value
  if (language.value !== 'en') return format(value, args)
  if (Object.prototype.hasOwnProperty.call(english, value)) return format(english[value], args)
  if (/[\u4e00-\u9fff]/.test(value)) {
    for (const pattern of patterns) {
      const match = pattern.regex.exec(value)
      if (match) return format(pattern.value, match.slice(1))
    }
  }
  return format(value, args)
}
