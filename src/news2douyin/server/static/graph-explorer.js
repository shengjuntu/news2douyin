const KINDS = {
  focus: ['焦点', '#ff9c77', [.99, .61, .47, 1]],
  watch: ['焦点', '#ff9c77', [.99, .61, .47, 1]],
  question: ['问题', '#f2cc79', [.95, .80, .47, 1]],
  outline_point: ['纲要', '#f2cc79', [.95, .80, .47, 1]],
  interest_scope: ['关注范围', '#ff9c77', [.99, .61, .47, 1]],
  interest_entity: ['关注实体', '#e8b0d3', [.91, .69, .83, 1]],
  interest_pattern: ['关注条件', '#ff9c77', [.99, .61, .47, 1]],
  event: ['事件', '#62d2e1', [.38, .82, .88, 1]],
  event_mention: ['事件提及', '#62d2e1', [.38, .82, .88, 1]],
  occurrence_candidate: ['事件线索', '#62d2e1', [.38, .82, .88, 1]],
  source_event: ['来源事件', '#72c4db', [.45, .77, .86, 1]],
  gdelt_actor: ['GDELT 行动者', '#e8b0d3', [.91, .69, .83, 1]],
  gdelt_place: ['GDELT 地点', '#9ace9a', [.60, .81, .60, 1]],
  gdelt_document: ['GDELT 文档', '#91a9f8', [.57, .66, .97, 1]],
  gkg_subject: ['GKG 主题', '#f2cc79', [.95, .80, .47, 1]],
  attention_topic: ['热榜话题', '#ff9c77', [.99, .61, .47, 1]],
  attention_source_item: ['热榜条目', '#91a9f8', [.57, .66, .97, 1]],
  article: ['报道', '#91a9f8', [.57, .66, .97, 1]],
  report: ['报道', '#91a9f8', [.57, .66, .97, 1]],
  source: ['来源', '#91a9f8', [.57, .66, .97, 1]],
  evidence: ['证据', '#9ace9a', [.60, .81, .60, 1]],
  evidence_ref: ['证据引用', '#9ace9a', [.60, .81, .60, 1]],
  claim: ['事实主张', '#cfabf4', [.81, .67, .96, 1]],
  investigation: ['研究', '#cfabf4', [.81, .67, .96, 1]],
  research_question: ['研究问题', '#f2cc79', [.95, .80, .47, 1]],
  argument: ['论据', '#cfabf4', [.81, .67, .96, 1]],
  person: ['人物', '#e8b0d3', [.91, .69, .83, 1]],
  organization: ['组织', '#cfabf4', [.81, .67, .96, 1]],
  location: ['地点', '#9ace9a', [.60, .81, .60, 1]],
  country: ['国家与地区', '#9ace9a', [.60, .81, .60, 1]],
  topic: ['议题', '#f2cc79', [.95, .80, .47, 1]],
  entity: ['实体', '#e8b0d3', [.91, .69, .83, 1]],
  referent: ['所指对象', '#e8b0d3', [.91, .69, .83, 1]],
}
const OTHER = ['节点', '#a3b1c6', [.64, .69, .78, 1]]
const RELATIONS = {
  action_at: '发生地', answered_by: '由此回答', asks: '提出问题', checked_by: '由此核查', cites: '引用',
  candidate_focus: '可能关联焦点', candidate_match: '候选匹配', coded_action_at: '编码发生地',
  coded_actor1: '编码行动方', coded_actor2: '编码对象方', gkg_mentions: 'GKG 提及',
  defined_by: '由此定义', describes: '描述', found: '发现', has_anchor: '包含关注对象', has_argument: '有论据',
  has_evidence: '有证据', has_match_key: '对应匹配键', has_material: '收录材料', has_pattern: '应用条件',
  has_source: '有来源', has_subquestion: '包含子问题', impacts: '影响地区', maps_to: '映射到',
  mentioned_in: '被提及于', mentions: '提及', mentions_place: '提及地点', observed_item: '榜单观察',
  pattern_object: '条件对象', pattern_subject: '条件主体',
  produced: '产出', refers_to: '指代', related_event: '关联事件', relevant_material: '相关材料',
  tracks: '跟踪', watches: '关注',
}
const DETAIL_NAMES = {
  article: '报道', code: '代码', domain: '来源域名', evidence: '证据', identity: '实体状态',
  quote: '原文摘录', reason: '收录依据', relation_type: '关系类型', revision: '原文版本', role: '角色',
  scope: '关注范围', seen: '最近更新', status: '状态', type: '类型', weight: '关联权重',
}

function text(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function normalized(value) {
  return text(value).replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().replace(/[\s-]+/g, '_')
}

function kindInfo(kind) {
  const key = normalized(kind)
  if (key === 'org') return KINDS.organization
  if (key === 'place') return KINDS.location
  if (Object.hasOwn(KINDS, key)) return KINDS[key]
  return key ? [kind, ...OTHER.slice(1)] : OTHER
}

function relationLabel(kind) {
  const key = normalized(kind)
  return Object.hasOwn(RELATIONS, key) ? RELATIONS[key] : text(kind) || '关联'
}

function element(tag, value = '', className = '') {
  const node = document.createElement(tag)
  if (value) node.textContent = value
  if (className) node.className = className
  return node
}

function safeLink(value) {
  const link = text(value)
  try {
    if (!link || link.includes('\\')) return null
    const target = new URL(link, window.location.href)
    if (!['https:', 'http:'].includes(target.protocol) || target.username || target.password) return null
    return target
  } catch {
    return null
  }
}

function showDetails(parent, details) {
  if (typeof details === 'string') {
    if (details) parent.append(element('p', details, 'graph-explorer-node-description'))
    return
  }
  if (!details || typeof details !== 'object') return
  const rows = element('dl', '', 'graph-explorer-data')
  for (const [key, value] of Object.entries(details)) {
    const label = Object.hasOwn(DETAIL_NAMES, key) ? DETAIL_NAMES[key] : key
    if (!['string', 'number'].includes(typeof value) || value === '') continue
    rows.append(element('dt', label), element('dd', String(value)))
  }
  if (rows.childElementCount) parent.append(rows)
}

function unpack(data) {
  if (!Array.isArray(data?.nodes) || !Array.isArray(data?.edges)) throw new Error('图谱数据格式不完整。')
  const nodes = []
  const indices = new Map()
  const neighbors = []
  for (const input of data.nodes) {
    const id = text(input?.id)
    if (!id || indices.has(id)) continue
    const kind = text(input.kind)
    const style = kindInfo(kind)
    indices.set(id, nodes.length)
    nodes.push({id, title: text(input.title) || id, kind: normalized(kind), style, url: input.url, details: input.details})
    neighbors.push([])
  }

  const edges = []
  for (const input of data.edges) {
    const source = indices.get(input?.source)
    const target = indices.get(input?.target)
    if (source === undefined || target === undefined) continue
    const index = edges.length
    edges.push({source, target, label: relationLabel(input.kind), details: input.details})
    neighbors[source].push({index, node: target, outgoing: true})
    if (source !== target) neighbors[target].push({index, node: source, outgoing: false})
  }
  return {nodes, neighbors, edges}
}

function installGraph(root) {
  const control = id => root.querySelector('#' + id)
  const canvas = control('graph-canvas')
  const query = control('graph-search')
  const results = control('graph-nodes')
  const details = control('graph-detail')
  const legend = control('graph-types')
  const status = control('graph-status')
  const summary = control('graph-summary')
  const fitButton = control('graph-fit')
  const zoomIn = control('graph-zoom-in')
  const zoomOut = control('graph-zoom-out')
  const clear = control('graph-clear')
  const movement = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 180
  const tooltip = element('span', '', 'graph-explorer-tooltip')
  tooltip.hidden = true
  tooltip.setAttribute('aria-hidden', 'true')
  canvas.append(tooltip)

  let nodes = [], neighbors = [], edges = [], order = [], matches = []
  let selected, filter = '', pageSize = 24
  let graph
  const resultButtons = new Map()
  const typeButtons = new Map()
  let metadataNotice = ''

  function report(extra = '') {
    status.textContent = [metadataNotice, extra].filter(Boolean).join(' ')
    status.hidden = !status.textContent
  }

  function markSelected() {
    for (const [index, button] of resultButtons) {
      if (index === selected) button.setAttribute('aria-current', 'true')
      else button.removeAttribute('aria-current')
    }
    clear.disabled = selected === undefined
  }

  function updateHighlights() {
    if (!graph) return
    let points
    let links
    if (selected !== undefined) {
      points = [...new Set([selected, ...neighbors[selected].map(row => row.node)])]
      links = neighbors[selected].map(row => row.index)
    } else if (filter || query.value.trim()) {
      points = matches
      const included = new Set(points)
      links = []
      for (const [index, edge] of edges.entries()) {
        if (included.has(edge.source) || included.has(edge.target)) links.push(index)
      }
    }
    graph.setConfigPartial({
      highlightedPointIndices: points,
      highlightedLinkIndices: links,
      outlinedPointIndices: selected === undefined ? undefined : [selected],
    })
  }

  function select(index, center = false, focusPanel = false) {
    selected = nodes[index] ? index : undefined
    markSelected()
    updateHighlights()
    renderDetail()
    if (focusPanel) details.querySelector('h3')?.focus()
    if (center && graph && selected !== undefined) {
      graph.zoomToPointByIndex(selected, movement, Math.max(1.5, graph.getZoomLevel()), false, false)
    }
  }

  function rowButton(index, onClick) {
    const node = nodes[index]
    const button = element('button', '', 'graph-explorer-node')
    button.type = 'button'
    const dot = element('span', '', 'graph-explorer-dot')
    dot.style.backgroundColor = node.style[1]
    dot.setAttribute('aria-hidden', 'true')
    button.append(dot, element('span', node.title), element('small', node.style[0]))
    button.addEventListener('click', onClick)
    return button
  }

  function renderDetail() {
    details.replaceChildren()
    if (selected === undefined) {
      const prompt = nodes.length ? '选择一个节点查看关系。' : '此图暂无可查看节点。'
      details.append(element('p', prompt, 'graph-explorer-muted'))
      return
    }
    const node = nodes[selected]
    const title = element('h3', node.title)
    title.tabIndex = -1
    details.append(element('span', node.style[0], 'graph-explorer-muted'), title)
    showDetails(details, node.details)
    const target = safeLink(node.url)
    if (target) {
      const anchor = element('a', target.origin === window.location.origin ? '打开详情' : '打开原文 ↗')
      anchor.href = target.href
      if (target.origin !== window.location.origin) {
        anchor.target = '_blank'
        anchor.rel = 'noopener noreferrer'
      }
      details.append(anchor)
    }
    const related = neighbors[selected]
    details.append(element('p', related.length ? `直接关系 ${related.length}` : '图中暂无直接关系。', 'graph-explorer-related-title'))
    if (!related.length) return

    const list = element('ul', '', 'graph-explorer-list')
    for (const row of related) {
      const edge = edges[row.index]
      const item = element('li')
      const relation = element('p', `${row.outgoing ? '指向' : '来自'} · ${edge.label}`, 'graph-explorer-relation')
      item.append(relation, rowButton(row.node, () => select(row.node, true, true)))
      showDetails(item, edge.details)
      list.append(item)
    }
    details.append(list)
  }

  function updateResults(restore = false) {
    resultButtons.clear()
    results.replaceChildren()
    const list = element('ul', '', 'graph-explorer-list')
    results.append(list)
    const fragment = document.createDocumentFragment()
    for (const index of matches.slice(0, pageSize)) {
      const item = element('li')
      const button = rowButton(index, () => select(index, true, true))
      item.append(button)
      fragment.append(item)
      resultButtons.set(index, button)
    }
    list.append(fragment)
    markSelected()
    if (!matches.length && nodes.length) results.append(element('p', '暂无匹配节点。', 'graph-explorer-muted'))
    if (matches.length > pageSize) {
      const more = element('button', `查看更多（${pageSize}/${matches.length}）`, 'ghost graph-explorer-more')
      more.type = 'button'
      more.addEventListener('click', () => {
        const next = matches[pageSize]
        pageSize += 24
        updateResults(next)
      })
      results.append(more)
    }
    if (restore !== false) resultButtons.get(restore)?.focus()
  }

  function search() {
    const words = query.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
    matches = order.filter(index => {
      const node = nodes[index]
      if (filter && node.kind !== filter) return false
      const haystack = `${node.title} ${node.style[0]} ${node.kind}`.toLocaleLowerCase()
      return words.every(word => haystack.includes(word))
    })
    for (const [kind, button] of typeButtons) button.setAttribute('aria-pressed', String(kind === filter))
    if (selected !== undefined && !matches.includes(selected)) select(undefined)
    pageSize = 24
    updateResults()
    updateHighlights()
  }

  function renderLegend() {
    const kinds = new Map()
    for (const node of nodes) {
      if (!kinds.has(node.kind)) kinds.set(node.kind, {style: node.style, count: 0})
      kinds.get(node.kind).count++
    }
    const items = [['', {style: ['全部'], count: nodes.length}], ...kinds]
    for (const [kind, {style, count}] of items) {
      const button = element('button', '', 'graph-explorer-type')
      button.type = 'button'
      button.setAttribute('aria-pressed', String(kind === filter))
      if (kind) {
        const dot = element('span', '', 'graph-explorer-dot')
        dot.style.backgroundColor = style[1]
        dot.setAttribute('aria-hidden', 'true')
        button.append(dot)
      }
      button.append(element('span', style[0]), element('small', String(count)))
      button.addEventListener('click', () => {
        filter = kind
        search()
      })
      legend.append(button)
      typeButtons.set(kind, button)
    }
  }

  function fit() {
    if (!graph || !matches.length) return
    if (matches.length === 1) graph.zoomToPointByIndex(matches[0], movement, 1.5, true, false)
    else if (matches.length < nodes.length) graph.fitViewByPointIndices(matches, movement, .18, false)
    else graph.fitView(movement, .18, false)
  }

  function zoom(scale) {
    if (!graph) return
    const level = Math.min(48, Math.max(.06, graph.getZoomLevel() * scale))
    graph.setZoomLevel(level, movement, false)
  }

  function disableGraphic(notice) {
    try {
      graph?.destroy()
    } catch {
      // A lost graphics context can make disposal fail; the HTML remains usable.
    }
    graph = undefined
    for (const button of [fitButton, zoomIn, zoomOut]) button.disabled = true
    root.dataset.graphic = 'unavailable'
    report(notice)
  }

  function hover(index, _position, event) {
    if (!nodes[index] || !event || !Number.isFinite(event.clientX)) {
      tooltip.hidden = true
      return
    }
    const bounds = canvas.getBoundingClientRect()
    tooltip.textContent = nodes[index].title
    tooltip.hidden = false
    const left = Math.max(4, Math.min(event.clientX - bounds.left + 12, bounds.width - tooltip.offsetWidth - 4))
    const top = Math.max(4, Math.min(event.clientY - bounds.top + 12, bounds.height - tooltip.offsetHeight - 4))
    tooltip.style.transform = `translate(${left}px, ${top}px)`
  }

  async function createGraphic() {
    try {
      const versioned = new URL(root.dataset.module, window.location.href)
      if (versioned.origin !== window.location.origin) throw new Error('图形引擎来源无效。')
      const loader = await import(versioned.href)
      const Graph = loader.Graph || globalThis.Cosmos?.Graph
      if (typeof Graph !== 'function') throw new Error('图形引擎不可用。')
      const instance = new Graph(canvas, {
        backgroundColor: '#101722',
        pointDefaultStrokeWidth: .9,
        pointGreyoutOpacity: .37,
        linkDefaultColor: '#7190b7',
        linkDefaultWidth: 1.1,
        linkOpacity: .62,
        linkGreyoutOpacity: .19,
        linkDefaultArrows: nodes.length <= 110,
        linkArrowsSizeScale: .72,
        renderHoveredPointRing: true,
        outlinedPointRingColor: '#ffffff',
        hoveredPointCursor: 'pointer',
        enableDrag: true,
        enableSimulation: nodes.length > 1,
        fitViewOnInit: nodes.length > 1,
        fitViewPadding: .18,
        fitViewDelay: 650,
        fitViewDuration: movement,
        initialZoomLevel: nodes.length === 1 ? 1.5 : undefined,
        rescalePositions: false,
        randomSeed: 17,
        onClick: index => select(index),
        onMouseMove: hover,
      })
      graph = instance
      await instance.ready

      const positions = new Float32Array(nodes.length * 2)
      const colors = new Float32Array(nodes.length * 4)
      const sizes = new Float32Array(nodes.length)
      const links = new Float32Array(edges.length * 2)
      const scale = Math.max(200, 24 * Math.sqrt(nodes.length))
      for (const [index, node] of nodes.entries()) {
        const angle = index * Math.PI * (3 - Math.sqrt(5))
        const radius = scale * Math.sqrt(index / nodes.length)
        positions.set([2048 + radius * Math.cos(angle), 2048 + radius * Math.sin(angle)], 2 * index)
        colors.set(node.style[2], 4 * index)
        sizes[index] = Math.min(23, 10 + 2 * Math.sqrt(neighbors[index].length))
      }
      for (const [index, edge] of edges.entries()) links.set([edge.source, edge.target], 2 * index)
      instance.setPointPositions(positions)
      instance.setPointColors(colors)
      instance.setPointSizes(sizes)
      instance.setLinks(links)
      instance.render()
      updateHighlights()
      canvas.querySelector('canvas')?.addEventListener('webglcontextlost', () => {
        disableGraphic('图形连接已中断。你仍可搜索节点和查看关系。')
      }, {once: true})
      for (const button of [fitButton, zoomIn, zoomOut]) button.disabled = false
      root.dataset.graphic = 'ready'
      report()
    } catch {
      disableGraphic('图形模式不可用。你仍可搜索节点和查看关系。')
    }
  }

  window.addEventListener('pagehide', event => {
    if (!event.persisted && graph) graph.destroy()
  })
  fitButton.disabled = zoomIn.disabled = zoomOut.disabled = clear.disabled = true
  query.disabled = true
  query.addEventListener('input', search)
  query.addEventListener('keydown', event => {
    if (event.key === 'Enter' && matches.length) {
      event.preventDefault()
      select(matches[0], true, true)
    }
  })
  clear.addEventListener('click', () => select(undefined))
  fitButton.addEventListener('click', fit)
  zoomIn.addEventListener('click', () => zoom(1.25))
  zoomOut.addEventListener('click', () => zoom(.8))
  canvas.addEventListener('pointerleave', () => { tooltip.hidden = true })
  canvas.addEventListener('keydown', event => {
    const action = { '+': () => zoom(1.25), '-': () => zoom(.8), Home: fit, Escape: () => select(undefined) }[event.key]
    if (typeof action !== 'function') return
    event.preventDefault()
    action()
  })

  async function load() {
    try {
      const response = await fetch(root.dataset.endpoint, {headers: {Accept: 'application/json'}})
      if (!response.ok) throw new Error('无法读取图谱。')
      const data = await response.json()
      ;({nodes, neighbors, edges} = unpack(data))
      const counts = `${nodes.length} 个节点 · ${edges.length} 条关系`
      summary.textContent = data.backend === 'falkordb' ? `${counts} · FalkorDB` : `${counts} · 关系库快照`
      const reasons = {
        unconfigured: '尚未配置图数据库，当前显示关系库数据。',
        pending: '图谱投影尚未完成，当前显示关系库数据。',
        unavailable: '图数据库暂时不可用，当前显示关系库数据。',
      }
      metadataNotice = [
        data.state === 'degraded' ? (reasons[data.reason] || '当前显示关系库数据。') : '',
        data.truncated ? '当前显示部分节点或关系。' : '',
      ].filter(Boolean).join(' ')
      renderDetail()
      if (!nodes.length) {
        root.dataset.graphic = 'empty'
        report('所选范围暂无可显示的图谱。')
        return
      }
      order = nodes.map((_, index) => index).sort((a, b) => neighbors[b].length - neighbors[a].length || a - b)
      query.disabled = false
      renderLegend()
      search()
      root.dataset.graphic = 'loading'
      report('正在启动交互图…')
      await createGraphic()
    } catch {
      root.dataset.graphic = 'unavailable'
      report('无法读取图谱。请刷新页面后再试。')
    }
  }

  load()
}

const explorer = document.getElementById('graph-explorer')
if (explorer) installGraph(explorer)
