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
  source_event: ['GDELT 事件', '#72c4db', [.45, .77, .86, 1]],
  gdelt_actor: ['GDELT 行动者', '#e8b0d3', [.91, .69, .83, 1]],
  gdelt_place: ['GDELT 地点', '#9ace9a', [.60, .81, .60, 1]],
  gdelt_document: ['报道', '#91a9f8', [.57, .66, .97, 1]],
  gkg_subject: ['GKG 主题', '#f2cc79', [.95, .80, .47, 1]],
  attention_topic: ['热榜话题', '#ff9c77', [.99, .61, .47, 1]],
  attention_source_item: ['热榜条目', '#91a9f8', [.57, .66, .97, 1]],
  article: ['报道', '#91a9f8', [.57, .66, .97, 1]],
  article_lead: ['报道线索', '#91a9f8', [.57, .66, .97, 1]],
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
  has_official_source: '采用的官方事件来源', extracted_mention: '原文陈述提及',
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
  scope: '关注范围', seen: '最近观测', status: '状态', type: '类型', weight: '关联权重',
  external_id: 'GlobalEventID', event_date: '官方事件日期', country: '国家或地区', basis: '焦点匹配依据',
  feed: '数据流', batch: '原始批次', row: '原始行号', identifier: '文档标识', gkg_record: 'GKG 记录',
  gkg_source: 'GKG 来源', archive_sha256: '归档 SHA-256', raw_sha256: '原始行 SHA-256',
  mention_key: 'Mentions 索引键', confidence: 'GDELT 提及置信度', sentence: '句号',
  provider: '采集来源', candidate: '采集候选编号', source_url: '报道网址', published_at: '报道发布时间',
  language: '语种', material_date: '最近材料日期', review: '陈述核对状态',
  observed: 'GDELT 首次收录', first_seen: '首次提及', first_feed: '首次数据流',
  first_batch: '首次批次', first_row: '首次原始行号',
  latitude: '纬度', longitude: '经度', quad_class: 'CAMEO 四分类', goldstein: 'Goldstein 分值',
}
const READ_REASONS = {
  unconfigured: '尚未配置图数据库。',
  pending: '图谱投影尚未完成。',
  query_timeout: '图查询超时。',
  socket_timeout: '等待图数据库返回超时。',
  query_error: '图数据库返回查询错误。',
  unavailable: '本次未能访问图数据库。',
  owner_mismatch: '图数据库的投影归属与当前实例不一致。',
  budget: '本次读取达到时间预算。',
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

const PROVIDERS = {worldnewsapi: 'World News API', newsapi: 'NewsAPI', newsnow: 'NewsNow',
  gdelt: 'GDELT', gkg: 'GDELT GKG', unknown: '来源未标记'}
const REVIEWS = {pending: '待核对', accepted: '已采用', rejected: '已驳回'}

function showDetails(parent, details) {
  if (typeof details === 'string') {
    if (details) parent.append(element('p', details, 'graph-explorer-node-description'))
    return
  }
  if (!details || typeof details !== 'object') return
  const rows = element('dl', '', 'graph-explorer-data')
  for (const [key, value] of Object.entries(details)) {
    const label = Object.hasOwn(DETAIL_NAMES, key) ? DETAIL_NAMES[key] : key
    if (!['string', 'number'].includes(typeof value) || value === '' || key === 'evidence_path') continue
    let display = value
    if (key === 'provider' && Object.hasOwn(PROVIDERS, value)) display = PROVIDERS[value]
    if (key === 'review' && Object.hasOwn(REVIEWS, value)) display = REVIEWS[value]
    const cell = element('dd', String(display))
    const source = key === 'source_url' && safeLink(value)
    if (source) {
      const link = element('a', '打开报道原网址 ↗')
      link.href = source.href
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
      cell.replaceChildren(link)
    }
    rows.append(element('dt', label), cell)
  }
  if (rows.childElementCount) parent.append(rows)
}

function evidenceList(rows) {
  const list = element('ol', '', 'graph-evidence-list')
  for (const row of rows) {
    const item = element('li')
    const date = text(row.time)
    const timeLabel = {reported_occurrence: '报道所述日期', published: '报道发布'}[row.time_kind]
    const metadata = [date && timeLabel ? timeLabel + ' ' + date.slice(0, 10) : '日期待核',
      text(row.status_label), text(row.role_label)].filter(Boolean)
    item.append(element('small', metadata.join(' · ')))
    const title = text(row.title) || text(row.event_title)
    if (title) item.append(element('p', title))
    if (text(row.evidence)) item.append(element('blockquote', row.evidence))
    const links = element('div', '', 'graph-evidence-links')
    for (const [path, prefix, label] of [[row.source_path, '/articles/', '报道原文'], [row.event_path, '/events/', '整理事件']]) {
      const target = safeLink(path)
      if (target?.origin !== window.location.origin || !target.pathname.startsWith(prefix)) continue
      if (prefix === '/events/') target.searchParams.set('back', window.location.pathname + window.location.search)
      const link = element('a', label)
      link.href = target.href
      links.append(link)
    }
    if (links.childElementCount) item.append(links)
    list.append(item)
  }
  return list
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
    nodes.push({id, title: text(input.title) || id, kind: normalized(kind), style,
      url: input.url, details: input.details, activities: input.activities || [], activityCount: input.activity_count})
    neighbors.push([])
  }

  const edges = []
  for (const input of data.edges) {
    const source = indices.get(input?.source)
    const target = indices.get(input?.target)
    if (source === undefined || target === undefined) continue
    const index = edges.length
    edges.push({source, target, label: text(input.label) || relationLabel(input.kind), details: input.details,
      directed: input.directed, evidence: input.evidence || [], evidenceCount: input.evidence_count})
    neighbors[source].push({index, node: target, outgoing: true})
    if (source !== target) neighbors[target].push({index, node: source, outgoing: false})
  }
  return {nodes, neighbors, edges}
}

function installGraph(root) {
  const peopleLens = root.dataset.lens === 'people'
  const focusGraph = Boolean(root.dataset.rebuild)
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
  let selected, selectedEdge, filter = '', pageSize = 24
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
    clear.disabled = selected === undefined && selectedEdge === undefined
  }

  function updateHighlights() {
    if (!graph) return
    let points
    let links
    if (selectedEdge !== undefined) {
      const edge = edges[selectedEdge]
      points = [edge.source, edge.target]
      links = [selectedEdge]
    } else if (selected !== undefined) {
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
    selectedEdge = undefined
    selected = nodes[index] ? index : undefined
    markSelected()
    updateHighlights()
    renderDetail()
    if (focusPanel) details.querySelector('h3')?.focus()
    if (center && graph && selected !== undefined) {
      graph.zoomToPointByIndex(selected, movement, Math.max(1.5, graph.getZoomLevel()), false, false)
    }
  }

  function selectEdge(index, focusPanel = false) {
    if (!edges[index]) return
    selected = undefined
    selectedEdge = index
    markSelected()
    updateHighlights()
    renderDetail()
    if (focusPanel) details.querySelector('h3')?.focus()
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

  function edgeButton(index, label) {
    const button = element('button', label, 'ghost graph-evidence-button')
    button.type = 'button'
    button.addEventListener('click', () => selectEdge(index, true))
    return button
  }

  function renderEdgeDetail(edge) {
    const title = element('h3', nodes[edge.source].title + (edge.directed ? ' → ' : ' · ') + nodes[edge.target].title)
    title.tabIndex = -1
    details.append(title, element('p', edge.label, 'graph-explorer-related-title'))
    const count = edge.evidenceCount || edge.evidence.length
    if (focusGraph) {
      showDetails(details, edge.details)
      const cite = safeLink(edge.details?.evidence_path)
      if (cite?.origin === window.location.origin && cite.pathname.startsWith('/articles/')) {
        const source = element('a', '查看对应版本的原文陈述')
        source.href = cite.href
        details.append(source)
      }
    }
    else details.append(element('h4', '关系证据'))
    if (edge.evidence.length) {
      if (count > edge.evidence.length) details.append(element('small', `显示 ${edge.evidence.length} / ${count} 条`))
      details.append(evidenceList(edge.evidence))
    } else if (!focusGraph) details.append(element('p', '这条关系暂无可查看的原文证据。'))
    const parallel = neighbors[edge.source].filter(row => row.node === edge.target && row.index !== selectedEdge)
    if (parallel.length) {
      details.append(element('h4', '两者的其他关系'))
      for (const row of parallel) details.append(edgeButton(row.index, edges[row.index].label))
    }
    for (const index of [edge.source, edge.target]) details.append(rowButton(index, () => select(index, true, true)))
  }

  function renderDetail() {
    details.replaceChildren()
    if (selectedEdge !== undefined) {
      renderEdgeDetail(edges[selectedEdge])
      return
    }
    if (selected === undefined) {
      if (peopleLens && !nodes.length) return
      const prompt = nodes.length ? (peopleLens ? '点人物看活动，点关系线看证据。' : '选择一个节点查看关系。') : '此图暂无可查看节点。'
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
      const local = target.origin === window.location.origin
      const anchor = element('a', peopleLens && node.kind === 'person' ? '查看人物时间线' : local ? '打开详情' : '打开原文 ↗')
      anchor.href = target.href
      if (!local) {
        anchor.target = '_blank'
        anchor.rel = 'noopener noreferrer'
      }
      const eventKey = root.dataset.focusKey && node.kind === 'event' && local && !target.search && !target.hash
        ? target.pathname.match(/^\/events\/([A-Za-z0-9_-]{1,160})$/)?.[1] : undefined
      if (eventKey || (focusGraph && node.kind === 'source_event')) {
        const investigate = element('button', '以此事件查前因', 'ghost')
        investigate.type = 'button'
        investigate.addEventListener('click', () => {
          document.dispatchEvent(new CustomEvent('n2d:causal-root', {detail: {eventKey: eventKey || '', title: node.title, eventDate: node.details?.event_date}}))
        })
        const actions = element('div', '', 'row')
        actions.append(anchor, investigate)
        details.append(actions)
      } else {
        details.append(anchor)
      }
    }
    if (!focusGraph && peopleLens && node.kind === 'person') {
      details.append(element('h4', '活动时间线'))
      if (node.activityCount > node.activities.length) {
        details.append(element('small', `显示 ${node.activities.length} / ${node.activityCount} 条活动`))
      }
      if (node.activities.length) details.append(evidenceList(node.activities))
      else details.append(element('p', '此范围暂无可查看的活动。', 'graph-explorer-muted'))
    }
    const related = neighbors[selected]
    details.append(element('p', related.length ? `直接关系 ${related.length}` : '图中暂无直接关系。', 'graph-explorer-related-title'))
    if (!related.length) return

    const list = element('ul', '', 'graph-explorer-list')
    for (const row of related) {
      const edge = edges[row.index]
      const item = element('li')
      if (focusGraph || peopleLens) {
        const label = nodes[row.node].title + ' · ' + edge.label
        item.append(edgeButton(row.index, label))
      } else {
        const relation = element('p', `${row.outgoing ? '指向' : '来自'} · ${edge.label}`, 'graph-explorer-relation')
        item.append(relation, rowButton(row.node, () => select(row.node, true, true)))
      }
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
    if (selectedEdge !== undefined && !matches.includes(edges[selectedEdge].source) && !matches.includes(edges[selectedEdge].target)) select(undefined)
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
        linkDefaultWidth: peopleLens ? 2.3 : 1.1,
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
        onPointClick: index => select(index),
        onBackgroundClick: () => select(undefined),
        onLinkClick: focusGraph || peopleLens ? index => selectEdge(index) : undefined,
        hoveredLinkCursor: focusGraph || peopleLens ? 'pointer' : 'auto',
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
      if (focusGraph || peopleLens) instance.setLinkArrows(edges.map(edge => edge.directed === true))
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

  const rebuild = control('graph-rebuild')
  const buildNotice = control('graph-build-status')
  const days = control('graph-days')
  const until = control('graph-until')
  const focus = control('graph-focus-key')
  let busy = false, pollTimer

  function showBuildNotice(message) {
    if (!buildNotice) return
    buildNotice.textContent = message
    buildNotice.hidden = !message
  }

  function buildState(data, polling = false) {
    const active = ['queued', 'building'].includes(data.state)
    busy = active
    rebuild.disabled = busy || focus.value !== root.dataset.focusKey
    if (active) {
      showBuildNotice('正在后台重建，完成后会自动显示新图。')
      clearTimeout(pollTimer)
      pollTimer = setTimeout(async () => {
        try {
          const response = await fetch(root.dataset.rebuild, {cache: 'no-store'})
          if (!response.ok) throw new Error()
          buildState(await response.json(), true)
        } catch {
          busy = false
          rebuild.disabled = focus.value !== root.dataset.focusKey
          showBuildNotice('暂时无法读取重建进度，可刷新页面。')
        }
      }, 2500)
    } else if (data.state === 'error') {
      showBuildNotice('重建失败，可以重新提交。' + (data.built_at ? '下方仍为最近一次完成的图。' : ''))
    } else if (polling) {
      window.location.reload()
    } else if (data.state === 'unbuilt') {
      showBuildNotice('选择事件日期后，点击「重建焦点图」。')
    } else {
      showBuildNotice('')
    }
  }

  focus?.addEventListener('change', () => {
    rebuild.disabled = busy || focus.value !== root.dataset.focusKey
  })
  rebuild?.addEventListener('click', async () => {
    if (busy || focus.value !== root.dataset.focusKey || !until.reportValidity()) return
    rebuild.disabled = busy = true
    showBuildNotice('正在提交重建…')
    try {
      const url = new URL(root.dataset.rebuild, window.location.href)
      url.searchParams.set('days', days.value)
      if (until.value) url.searchParams.set('until', until.value)
      const response = await fetch(url, {method:'POST', headers:{Accept:'application/json'}})
      if (!response.ok) throw new Error()
      buildState(await response.json())
    } catch {
      busy = false
      rebuild.disabled = focus.value !== root.dataset.focusKey
      showBuildNotice('无法提交重建，请重试。')
    }
  })

  async function load() {
    try {
      const response = await fetch(root.dataset.endpoint, {headers: {Accept: 'application/json'}})
      if (!response.ok) throw new Error('无法读取图谱。')
      const data = await response.json()
      ;({nodes, neighbors, edges} = unpack(data))
      const counts = `${nodes.length} 个节点 · ${edges.length} 条关系`
      const fromGraph = data.backend === 'falkordb'
      const scopeCounts = data.counts || {}
      const localCount = scopeCounts.local_events || 0
      const officialCount = scopeCounts.official_events ?? scopeCounts.events ?? 0
      const graphCounts = [`系统事件 ${localCount}`, `GDELT 事件 ${officialCount}`,
        `报道 ${scopeCounts.reports || 0}`, scopeCounts.report_leads ? `报道线索 ${scopeCounts.report_leads}` : '']
      summary.textContent = focusGraph
        ? [counts, ...graphCounts].filter(Boolean).join(' · ')
        : `${counts} · ${fromGraph ? 'FalkorDB' : '关系库快照'}`
      const scope = data.window
      const archive = data.gkg_archive
      metadataNotice = focusGraph ? [
        scope?.date_from && scope.date_to ? `图谱日期范围 ${scope.date_from} 至 ${scope.date_to}。` : '',
        archive?.used_urls ? `${archive.used_urls} 篇报道已由原始 GKG 归档补查。` : '',
        archive?.state === 'checked' && !nodes.length && archive?.date_from
          ? `GKG 归档核对 ${archive.date_from} 至 ${archive.date_to}（UTC）。` : '',
        data.truncated ? '当前显示部分事件或证据。' : '',
      ].filter(Boolean).join(' ') : [
        Object.hasOwn(READ_REASONS, data.reason) ? READ_REASONS[data.reason] : '',
        fromGraph && data.state === 'partial' ? '当前显示已经读取的图数据。' : '',
        !fromGraph && nodes.length ? '当前显示关系库快照。' : '',
        data.truncated ? '本次读取达到节点、关系或邻域上限。' : '',
      ].filter(Boolean).join(' ')
      if (focusGraph) {
        const params = new URL(window.location.href).searchParams
        if (data.requested && !params.has('days') && !params.has('until')) {
          days.value = String(data.requested.days)
          until.value = data.requested.until
        }
        buildState(data)
      }
      renderDetail()
      if (!nodes.length) {
        root.dataset.graphic = 'empty'
        report(focusGraph ? text(data.empty_reason) : '所选范围暂无可显示的图谱。')
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
