export function showCoverage(panel, scope, open) {
  if (!panel || !scope) return
  panel.hidden = !scope.sampled_events
  if (panel.hidden) return
  panel.open = open
  if (scope.date_to) panel.dataset.until = scope.date_to
  for (const node of panel.querySelectorAll('[data-count]')) {
    node.textContent = scope[node.dataset.count] ?? 0
  }
  for (const row of panel.querySelectorAll('[data-metric]')) {
    const key = row.dataset.metric
    const value = key.startsWith('extraction.') ? scope.extraction?.[key.slice(11)] : scope[key]
    row.hidden = !value
    row.querySelector('b').textContent = value ?? 0
  }
  const examples = panel.querySelector('[data-people-examples]')
  const links = examples.querySelector('ul')
  links.replaceChildren()
  for (const item of scope.extraction?.examples_without_person ?? []) {
    if (!item.source_path?.startsWith('/articles/')) continue
    const anchor = document.createElement('a')
    anchor.href = item.source_path
    anchor.textContent = item.title || '查看原文'
    const li = document.createElement('li')
    li.append(anchor)
    links.append(li)
  }
  examples.hidden = !links.childElementCount
  panel.querySelector('[data-people-repair]').hidden = !(scope.extraction?.repair_candidates > 0)
}

for (const panel of document.querySelectorAll('[data-people-coverage]')) {
  const button = panel.querySelector('[data-people-repair]')
  const status = panel.querySelector('[data-people-repair-status]')
  button.addEventListener('click', async () => {
    if (button.disabled || button.hidden) return
    button.disabled = true
    status.hidden = false
    status.textContent = '正在核对材料…'
    try {
      const url = new URL(panel.dataset.endpoint, window.location.origin)
      url.searchParams.set('days', panel.dataset.days)
      if (panel.dataset.until) url.searchParams.set('until', panel.dataset.until)
      const response = await fetch(url, {method: 'POST', headers: {Accept: 'application/json'}})
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '提交失败，请重试。')
      const notes = []
      if (result.queued) {
        let summary = `已安排后台抽取 ${result.queued} 个版本`
        if (result.worker_checks) summary += `（其中 ${result.worker_checks} 个大版本由后台先核对原文）`
        notes.push(summary)
      }
      if (result.pending) notes.push(`${result.pending} 个版本已在队列中或处理中`)
      if (result.done) notes.push(`${result.done} 个版本已处理或已有陈述`)
      if (result.failed) notes.push(`${result.failed} 个版本有失败记录`)
      if (result.unavailable) notes.push(`${result.unavailable} 个版本不具备补抽条件`)
      if (result.remaining) notes.push(`还有 ${result.remaining} 个版本可继续检查`)
      status.textContent = (notes.join('，') || '本次没有可补抽的版本') + '。'
      if (result.queued || result.pending) status.textContent += '可稍后刷新查看结果。'
      button.hidden = !result.remaining
    } catch (error) {
      status.textContent = error.message || '提交失败，请重试。'
    } finally {
      button.disabled = false
    }
  })
}
