'use strict';

// Node 22 built-ins and the existing Chrome binary only. No browser packages.
const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');
const RUNTIME = path.join(ROOT, '.runtime');
const ROLES = ['sales', 'manager', 'manager2', 'buyer', 'warehouse', 'finance', 'other_sales'];
const GROUPS = {sales: 'sales_team.group_sale_salesman', manager: 'sales_team.group_sale_manager',
  manager2: 'sales_team.group_sale_manager', buyer: 'purchase.group_purchase_user',
  warehouse: 'stock.group_stock_user', finance: 'account.group_account_invoice',
  other_sales: 'sales_team.group_sale_salesman'};
const MODELS = {sale_order: 'sale.order', purchase_order: 'purchase.order',
  receipt_first: 'stock.picking', receipt_second: 'stock.picking',
  delivery_first: 'stock.picking', delivery_second: 'stock.picking', return_picking: 'stock.picking',
  invoice_first: 'account.move', invoice_second: 'account.move', credit_note: 'account.move'};
const MENU_MODELS = {sales: 'sale.order', purchases: 'purchase.order', stock: 'stock.picking', invoices: 'account.move'};
const ROLE_MENUS = {sales: ['sales'], manager: ['sales'], manager2: ['sales'], buyer: ['purchases'],
  warehouse: ['stock'], finance: ['sales', 'invoices'], other_sales: ['sales']};
const PHYSICAL = {gross_delivered_qty: 100, returned_qty: 10, net_delivered_qty: 90};
const FINANCIAL = ['draft_invoice_qty', 'posted_invoice_qty', 'draft_credit_qty',
  'posted_credit_qty', 'posted_signed_amount', 'invoice_ids', 'credit_note_ids'];
const SALES_BUTTONS = ['action_confirm', 'action_quotation_send', 'action_cancel', 'action_draft',
  'action_portfolio_submit', 'action_portfolio_approve', 'action_portfolio_reject_wizard'];

function requireEvidence(condition) {
  if (!condition) throw new Error('Local browser evidence did not satisfy the acceptance contract.');
}

function sameMembers(actual, expected) {
  return Array.isArray(actual) && actual.length === expected.length &&
    [...actual].sort().every((item, index) => item === [...expected].sort()[index]);
}

function positiveId(value) { return Number.isInteger(value) && value > 0; }

function safeRuntimePath(value, existing = true) {
  requireEvidence(typeof value === 'string' && value.length > 0);
  const result = path.resolve(ROOT, value);
  const relative = path.relative(RUNTIME, result);
  requireEvidence(relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative));
  for (let current = result; current !== ROOT; current = path.dirname(current)) {
    let info;
    try { info = fs.lstatSync(current); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (info) requireEvidence(!info.isSymbolicLink());
  }
  if (existing) requireEvidence(fs.statSync(result).isFile());
  return result;
}

function validateTarget(baseUrl, database) {
  const target = new URL(baseUrl);
  requireEvidence(target.protocol === 'http:' && target.hostname === '127.0.0.1' &&
    target.port !== '' && Number(target.port) > 0 && target.pathname === '/' &&
    !target.username && !target.password && !target.search && !target.hash &&
    !baseUrl.endsWith('/') && ['odoo_portfolio', 'odoo_portfolio_m1_test'].includes(database));
}

function parseArgs(argv) {
  const names = {'--base-url': 'baseUrl', '--database': 'database', '--credentials': 'credentials',
    '--acceptance-report': 'acceptanceReport', '--flow-report': 'flowReport', '--role-report': 'roleReport',
    '--output-dir': 'outputDir', '--chrome-path': 'chromePath'};
  const options = {outputDir: path.join(RUNTIME, 'm2c-browser')};
  requireEvidence(argv.length % 2 === 0);
  const seen = new Set();
  for (let index = 0; index < argv.length; index += 2) {
    const key = names[argv[index]];
    requireEvidence(key && !seen.has(key) && argv[index + 1] && !argv[index + 1].startsWith('--'));
    seen.add(key);
    options[key] = argv[index + 1];
  }
  for (const key of ['baseUrl', 'database', 'credentials', 'acceptanceReport', 'flowReport', 'roleReport']) {
    requireEvidence(typeof options[key] === 'string');
  }
  validateTarget(options.baseUrl, options.database);
  for (const key of ['credentials', 'acceptanceReport', 'flowReport', 'roleReport']) {
    options[key] = safeRuntimePath(options[key], false);
  }
  options.outputDir = safeRuntimePath(options.outputDir, false);
  return options;
}

function readJson(filename) { return JSON.parse(fs.readFileSync(filename, 'utf8')); }

function readPrivateCredentials(filename, database) {
  const descriptor = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  let credentials;
  try {
    const info = fs.fstatSync(descriptor);
    requireEvidence(info.isFile() && (info.mode & 0o777) === 0o600 && info.uid === process.getuid());
    credentials = JSON.parse(fs.readFileSync(descriptor, 'utf8'));
  } finally {
    fs.closeSync(descriptor);
  }
  requireEvidence(sameMembers(Object.keys(credentials), ['schema_version', 'database', 'accounts']) &&
    credentials.schema_version === 1 && credentials.database === database &&
    sameMembers(Object.keys(credentials.accounts), ROLES));
  const passwords = new Set();
  for (const role of ROLES) {
    const account = credentials.accounts[role];
    requireEvidence(sameMembers(Object.keys(account), ['login', 'password']) &&
      account.login === `portfolio.acceptance.${role}` && typeof account.password === 'string' &&
      account.password.length >= 24 && account.password.trim() && !passwords.has(account.password));
    passwords.add(account.password);
  }
  return credentials;
}

function validateSourceReports(inputs) {
  const identities = {company_a: 'res.company', company_b: 'res.company', warehouse_a: 'stock.warehouse',
    customer: 'res.partner', vendor: 'res.partner', initial_bearing_move: 'stock.move',
    ...Object.fromEntries(ROLES.map(role => [`user_${role}`, 'res.users'])),
    ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(key => [`product_${key}`, 'product.product'])),
    ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(key => [`supplier_${key}`, 'product.supplierinfo']))};
  requireEvidence(inputs.acceptance.namespace === 'portfolio_trade_acceptance' &&
    sameMembers(Object.keys(inputs.acceptance.identities), Object.keys(identities)));
  for (const [key, model] of Object.entries(identities)) {
    const item = inputs.acceptance.identities[key];
    requireEvidence(item.model === model && positiveId(item.id));
  }
  requireEvidence(new Set(ROLES.map(role => inputs.acceptance.identities[`user_${role}`].id)).size === 7 &&
    inputs.acceptance.identities.company_a.id !== inputs.acceptance.identities.company_b.id);
  const flow = inputs.flow;
  requireEvidence(flow.database === inputs.database && flow.namespace === 'portfolio_trade_fulfillment' &&
    flow.company_id === inputs.acceptance.identities.company_a.id &&
    sameMembers(Object.keys(flow.identities), Object.keys(MODELS)) &&
    sameMembers(Object.keys(flow.actors), ['sales', 'manager', 'buyer', 'warehouse', 'finance']));
  const unique = new Set();
  for (const [key, model] of Object.entries(MODELS)) {
    const item = flow.identities[key];
    requireEvidence(item.model === model && positiveId(item.id) && typeof item.name === 'string' &&
      item.name.trim() && item.name.length < 200 && !/[\x00-\x1f]/.test(item.name));
    unique.add(`${model}:${item.id}`);
  }
  requireEvidence(unique.size === 10);
  for (const [role, uid] of Object.entries(flow.actors)) {
    requireEvidence(uid === inputs.acceptance.identities[`user_${role}`].id);
  }
  for (const [key, value] of Object.entries({delivered: 100, returned: 10, net: 90, stock: 10})) {
    requireEvidence(flow.physical[key] === value);
  }
  for (const [key, value] of Object.entries({invoice_posted: 100, credit_posted: 10,
    signed_posted: 90, net_amount: 7650, currency: 'CNY'})) requireEvidence(flow.financial[key] === value);
}

function validateRoleReport(report, inputs) {
  validateSourceReports(inputs);
  requireEvidence(report.ok === true && report.schema_version === 1 && report.database === inputs.database &&
    sameMembers(Object.keys(report.roles), ROLES));
  for (const role of ROLES) {
    const item = report.roles[role];
    const company = inputs.acceptance.identities[role === 'other_sales' ? 'company_b' : 'company_a'].id;
    requireEvidence(item.uid === inputs.acceptance.identities[`user_${role}`].id && item.http_status === 200 &&
      item.company_id === company && sameMembers(item.allowed_company_ids, [company]) &&
      sameMembers(item.active_company_ids, [company]) && item.checks.authenticated === true &&
      item.checks.company_scope === true && item.checks.native_records_read === true);
    if (item.groups) requireEvidence(item.groups.includes(GROUPS[role]) && !item.groups.includes('base.group_system'));
    for (const key of ROLE_MENUS[role]) {
      const menu = item.menus[key], action = item.actions[key];
      requireEvidence(positiveId(menu.id) && positiveId(menu.action_id) && menu.action_id === action.id &&
        menu.xmlid === `portfolio_trade.menu_portfolio_${key}` && menu.model === MENU_MODELS[key] &&
        action.res_model === MENU_MODELS[key] && ['list', 'form'].every(mode => action.view_mode.split(',').includes(mode)));
    }
    for (const [key, id] of Object.entries(item.documents)) {
      requireEvidence(inputs.flow.identities[key] && inputs.flow.identities[key].id === id && role !== 'other_sales');
    }
    if (role !== 'warehouse') requireEvidence(typeof item.source_route === 'string' &&
      /^\/odoo\/(?:action-[\w.]+|sale\.order)\/\d+$/.test(item.source_route) &&
      item.source_route.endsWith(`/${inputs.flow.identities.sale_order.id}`));
  }
  return report;
}

function readInputs(options) {
  validateTarget(options.baseUrl, options.database);
  for (const key of ['credentials', 'acceptanceReport', 'flowReport', 'roleReport']) safeRuntimePath(options[key]);
  safeRuntimePath(options.outputDir, false);
  const inputPaths = ['credentials', 'acceptanceReport', 'flowReport', 'roleReport'].map(key => path.resolve(ROOT, options[key]));
  requireEvidence(!inputPaths.includes(path.resolve(ROOT, options.outputDir)));
  if (fs.existsSync(options.outputDir)) requireEvidence(fs.statSync(options.outputDir).isDirectory());
  const inputs = {database: options.database, acceptance: readJson(options.acceptanceReport),
    flow: readJson(options.flowReport), roleReport: readJson(options.roleReport)};
  validateRoleReport(inputs.roleReport, inputs);
  inputs.credentials = readPrivateCredentials(options.credentials, options.database);
  for (const filename of ['report.json', ...ROLES.map(role => `${role}.png`)]) {
    const destination = safeRuntimePath(path.join(options.outputDir, filename), false);
    requireEvidence(!inputPaths.includes(destination));
    if (fs.existsSync(destination)) requireEvidence(fs.statSync(destination).isFile());
  }
  return inputs;
}

function numericText(value) {
  requireEvidence(typeof value === 'string' && /\d/.test(value));
  return Number(value.replaceAll(',', '').replaceAll('\u00a0', '').replace(/[^\d.-]/g, ''));
}

function expectedTraceNames(role, inputs) {
  const report = inputs.roleReport.roles[role];
  return Object.keys(report.trace_documents || report.documents).filter(key => key !== 'sale_order' &&
    (role === 'finance' || MODELS[key] !== 'account.move')).map(key => inputs.flow.identities[key].name);
}

function nativeEmptyState(state) {
  return state && state.visible === true && state.selector === '.o_view_nocontent .o_nocontent_help' &&
    typeof state.text === 'string' && state.text.trim().length > 0;
}

function classifyListRows(names, hasNativeSampleData, emptyState, role, inputs) {
  requireEvidence(Array.isArray(names) && names.every(name => typeof name === 'string') &&
    typeof hasNativeSampleData === 'boolean');
  if (!hasNativeSampleData) return {record_names: names, has_native_sample_data: false, sample_row_count: 0};
  requireEvidence(role === 'other_sales' && nativeEmptyState(emptyState) && names.length > 0 &&
    Object.keys(inputs.roleReport.roles[role].documents).length === 0 &&
    !Object.values(inputs.flow.identities).some(item => names.includes(item.name)));
  return {record_names: [], has_native_sample_data: true, sample_row_count: names.length};
}

function validateSampleEvidence(page, role) {
  if (page.has_native_sample_data === true) {
    requireEvidence(role === 'other_sales' && nativeEmptyState(page.empty_state) &&
      Number.isInteger(page.sample_row_count) && page.sample_row_count > 0 && page.sample_row_count <= 1000 &&
      page.record_names.length === 0);
  } else requireEvidence(page.sample_row_count === undefined || page.sample_row_count === 0);
}

function validateListEvidence(key, evidence, inputs, role) {
  requireEvidence(ROLE_MENUS[role].includes(key) && evidence.visible === true &&
    evidence.model === MENU_MODELS[key] && Array.isArray(evidence.columns) &&
    Array.isArray(evidence.record_names));
  validateSampleEvidence(evidence, role);
  const expected = Object.keys(inputs.roleReport.roles[role].documents)
    .filter(name => MODELS[name] === MENU_MODELS[key]).map(name => inputs.flow.identities[name].name);
  if (role === 'other_sales') {
    requireEvidence(key === 'sales' && expected.length === 0 && evidence.record_names.length === 0 &&
      nativeEmptyState(evidence.empty_state));
    return evidence;
  }
  requireEvidence(evidence.columns.includes('name') && !nativeEmptyState(evidence.empty_state));
  requireEvidence(evidence.column_visibility?.name === true &&
    Array.isArray(evidence.record_name_visibility) &&
    evidence.record_name_visibility.length === evidence.record_names.length &&
    evidence.record_name_visibility.every(value => value === true));
  requireEvidence(expected.every(name => evidence.record_names.includes(name)));
  if (key === 'stock') requireEvidence(['origin', 'state'].every(name => evidence.columns.includes(name) &&
    evidence.column_visibility[name] === true));
  if (key === 'invoices') requireEvidence(['move_type', 'invoice_date'].some(name => evidence.columns.includes(name) &&
    evidence.column_visibility[name] === true));
  return evidence;
}

function validateDomEvidence(role, evidence, inputs) {
  requireEvidence(ROLES.includes(role) && evidence.role === role && evidence.screenshot === `${role}.png`);
  const login = evidence.login, page = evidence.page;
  requireEvidence(login.rendered_form === true && login.submitted === true && login.authenticated === true &&
    login.user_label === `Acceptance ${role}` && login.password_field_count === 0);
  const report = inputs.roleReport.roles[role];
  const pageModel = role === 'warehouse' ? 'stock.picking' : role === 'buyer' ? 'purchase.order' : 'sale.order';
  requireEvidence(page.visible === true && page.model === pageModel &&
    Object.values(report.menus).every(menu => page.menu_labels.includes(menu.name)));
  const expectedPageNames = Object.keys(report.documents).filter(key => MODELS[key] === pageModel)
    .map(key => inputs.flow.identities[key].name);
  requireEvidence(Array.isArray(page.record_names) && expectedPageNames.every(name => page.record_names.includes(name)));
  if (role !== 'other_sales') requireEvidence(Array.isArray(page.record_name_visibility) &&
    page.record_name_visibility.length === page.record_names.length && page.record_name_visibility.every(value => value === true));
  validateSampleEvidence(page, role);
  const traceAllowed = !['warehouse', 'other_sales'].includes(role);
  if (role === 'other_sales') requireEvidence(page.record_names.length === 0 && page.foreign_source_absent === true &&
    nativeEmptyState(page.empty_state));
  if (!traceAllowed) {
    requireEvidence(evidence.source === null && evidence.trace === null);
  } else {
    const source = evidence.source, trace = evidence.trace;
    requireEvidence(source && source.visible === true && source.name === inputs.flow.identities.sale_order.name &&
      trace && trace.visible === true && trace.opened_by_click === true &&
      trace.source_name === inputs.flow.identities.sale_order.name);
    if (['buyer', 'finance'].includes(role)) requireEvidence(source.readonly === true && source.write_controls.length === 0);
    requireEvidence(Array.isArray(trace.line_rows) && trace.line_rows.length === 1 &&
      trace.line_rows[0].visible === true && trace.line_rows[0].unit_visible === true &&
      trace.line_rows[0].product_visible === true && trace.line_rows[0].unit.trim().length > 0 &&
      trace.line_rows[0].product.trim().length > 0);
    for (const [field, value] of Object.entries(PHYSICAL)) {
      requireEvidence(trace.quantity_visibility?.[field] === true && trace.line_rows[0].quantity_visibility?.[field] === true &&
        numericText(trace.quantities[field]) === value && numericText(trace.line_rows[0][field]) === value);
    }
    requireEvidence(sameMembers([...new Set(trace.linked_document_names)], [...new Set(expectedTraceNames(role, inputs))]));
    if (role === 'finance') {
      requireEvidence(FINANCIAL.every(name => trace.financial_field_names.includes(name)) && evidence.invoice &&
        evidence.invoice.visible === true && evidence.invoice.model === 'account.move' &&
        evidence.invoice.name === inputs.flow.identities.invoice_first.name &&
        numericText(trace.financial_values.posted_invoice_qty) === 100 &&
        numericText(trace.financial_values.posted_credit_qty) === 10 &&
        numericText(trace.financial_values.posted_signed_amount) === 7650);
    } else requireEvidence(trace.financial_field_names.length === 0 && Object.keys(trace.financial_values).length === 0);
  }
  return {role, screenshot: evidence.screenshot, uid: report.uid, company_id: report.company_id,
    menu_labels: [...page.menu_labels], model: pageModel,
    has_native_sample_data: page.has_native_sample_data === true, sample_row_count: page.sample_row_count || 0,
    checks: {rendered_login: true, actual_page: true, native_record_names: true,
      trace_clicked: traceAllowed, per_line_units: traceAllowed, financial_fields_scoped: true,
      readonly_source: ['buyer', 'finance'].includes(role), other_company_empty: role === 'other_sales'},
    facts: traceAllowed ? {...PHYSICAL, ...(role === 'finance' ? {posted_signed_amount: 7650} : {})} : {}};
}

function validateBrowserReport(evidence, inputs) {
  validateRoleReport(inputs.roleReport, inputs);
  requireEvidence(sameMembers(Object.keys(evidence), ROLES));
  return {schema_version: 1, ok: true, database: inputs.database, verification: 'automated rendered browser',
    roles: Object.fromEntries(ROLES.map(role => [role, validateDomEvidence(role, evidence[role], inputs)]))};
}

function sanitizeDiagnostics(raw) {
  if (!raw || typeof raw !== 'object') return {};
  const safe = {};
  for (const key of ['document_ready', 'navbar_present', 'navbar_visible', 'list_present', 'list_visible',
    'table_present', 'table_visible', 'empty_helper_present', 'empty_helper_visible',
    'form_present', 'form_visible', 'name_widget_present', 'name_widget_visible',
    'expected_name_matches', 'error_dialog_present']) {
    if (typeof raw[key] === 'boolean') safe[key] = raw[key];
  }
  for (const key of ['row_count', 'empty_help_text_length']) {
    if (Number.isInteger(raw[key]) && raw[key] >= 0 && raw[key] <= 1000000) safe[key] = raw[key];
  }
  const classes = new Set(['o_action_manager', 'o_action', 'o_list_view', 'o_kanban_view', 'o_form_view',
    'o_content', 'o_list_renderer', 'o_view_nocontent', 'o_nocontent_help', 'o_list_table', 'o_renderer',
    'o_view_controller', 'o_control_panel', 'o_view_sample_data', 'o_searchview', 'o_loading', 'o_web_client', 'o_error_dialog']);
  const columns = new Set(['name', 'origin', 'state', 'date_order', 'partner_id', 'user_id', 'invoice_status',
    'amount_total', 'amount_untaxed', 'currency_id', 'company_id', 'date', 'date_deadline', 'scheduled_date',
    'picking_type_id', 'location_id', 'location_dest_id', 'priority', 'invoice_date', 'invoice_date_due',
    'payment_state', 'move_type', 'activity_ids', 'reservation_date', 'availability', 'product_id',
    'reference', 'invoice_origin', 'invoice_partner_display_name', 'amount_residual']);
  const menus = new Set(['root', ...Object.keys(MENU_MODELS)].map(key => `portfolio_trade.menu_portfolio_${key}`));
  for (const [key, allowed] of [['view_classes', classes], ['native_columns', columns],
    ['visible_native_columns', columns], ['visible_menu_xmlids', menus]]) {
    if (Array.isArray(raw[key])) safe[key] = [...new Set(raw[key].filter(value => allowed.has(value)))].slice(0, 20);
  }
  return safe;
}

function chromeStartupDiagnostics(raw = {}) {
  const stderr = typeof raw.stderr === 'string' ? raw.stderr.slice(0, 8192) : '';
  let cause = 'unknown';
  if (/(?:ProcessSingleton|process_singleton|SingletonSocket)/i.test(stderr) &&
    /(?:File name too long|ENAMETOOLONG|(?:socket|path).*too long)/i.test(stderr)) cause = 'singleton_socket_path_too_long';
  else if (/(?:No usable sandbox|SUID sandbox|Running as root without --no-sandbox|Failed to move to new namespace)/i.test(stderr)) cause = 'sandbox';
  else if (/(?:failed|error|cannot|unable|no space).*(?:\/dev\/shm|shared.memory)|(?:\/dev\/shm|shared.memory).*(?:failed|error|cannot|unable|no space)/i.test(stderr)) cause = 'shared_memory';
  else if (['EACCES', 'EPERM'].includes(raw.spawn_code) || /(?:Permission denied|Operation not permitted|EACCES|EPERM)/i.test(stderr)) cause = 'permission';
  else if (['ENOENT', 'ENOEXEC'].includes(raw.spawn_code) || /(?:error while loading shared libraries|cannot open shared object file)/i.test(stderr)) cause = 'missing_binary_or_library';
  else if (raw.timed_out === true) cause = 'timeout';
  const binary = typeof raw.binary === 'string' ? path.basename(raw.binary) : '';
  const binaries = ['Google Chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];
  const signals = ['SIGTERM', 'SIGKILL', 'SIGABRT', 'SIGSEGV', 'SIGBUS', 'SIGILL', 'SIGTRAP', 'SIGSYS', 'SIGHUP', 'SIGINT'];
  return {cause, binary_basename: binaries.includes(binary) ? binary : 'unrecognized',
    exit_code: Number.isInteger(raw.exit_code) && raw.exit_code >= 0 && raw.exit_code <= 255 ? raw.exit_code : null,
    signal: signals.includes(raw.signal) ? raw.signal : null,
    duration_ms: Number.isFinite(raw.duration_ms) ? Math.max(0, Math.min(120000, Math.floor(raw.duration_ms))) : 0,
    port_file_present: raw.port_file_present === true, tmpdir_exists: raw.tmpdir_exists === true,
    debugging_loopback: raw.debugging_loopback === true};
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.sequence = 0;
    this.pending = new Map();
    socket.addEventListener('message', event => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error('Chrome command failed.'));
      else pending.resolve(message.result);
    });
    socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) {
        clearTimeout(pending.timer);
        pending.reject(new Error('Chrome connection closed.'));
      }
      this.pending.clear();
    });
  }

  static async connect(url) {
    requireEvidence(typeof global.WebSocket === 'function' && /^ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\//.test(url));
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { socket.close(); reject(new Error('Chrome connection timed out.')); }, 10000);
      socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, {once: true});
      socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Chrome connection failed.')); }, {once: true});
    });
    return new Cdp(socket);
  }

  call(method, params = {}, sessionId) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('Chrome command timed out.')); }, 15000);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    });
  }

  close() { this.socket.close(); }
}

class Page {
  constructor(cdp, sessionId) { this.cdp = cdp; this.sessionId = sessionId; }
  call(method, params) { return this.cdp.call(method, params, this.sessionId); }
  async evaluate(expression) {
    const response = await this.call('Runtime.evaluate', {expression, returnByValue: true,
      awaitPromise: true, userGesture: true});
    if (response.exceptionDetails) throw new Error('Rendered page inspection failed.');
    return response.result.value;
  }
  async wait(expression, timeout = 30000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const value = await this.evaluate(expression);
      if (value) return value;
      await delay(250);
    }
    throw new Error('Rendered page stage timed out.');
  }
  async navigate(url) {
    const result = await this.call('Page.navigate', {url});
    requireEvidence(!result.errorText);
  }
}

// DOM selectors and text only; no Odoo service calls or browser-side JSON-RPC.
const DOM_HELPERS = `const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
const text = el => (el?.textContent || '').trim();
const field = (root, name) => root.querySelector('.o_field_widget[name="' + name + '"]');
const fieldText = (root, name) => text(field(root, name));`;

function inspect(expression) { return `(() => { ${DOM_HELPERS} ${expression} })()`; }

async function login(page, role, options, inputs) {
  const redirect = '/odoo?debug=1';
  await page.navigate(`${options.baseUrl}/web/login?db=${encodeURIComponent(options.database)}&redirect=${encodeURIComponent(redirect)}`);
  await page.wait(inspect('return visible(document.querySelector("form.oe_login_form input[name=login]")) && visible(document.querySelector("input[name=password]"));'));
  const account = inputs.credentials.accounts[role];
  await page.evaluate(inspect(`const login = document.querySelector('input[name="login"]');
    const password = document.querySelector('input[name="password"]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(login, ${JSON.stringify(account.login)}); login.dispatchEvent(new Event('input', {bubbles: true}));
    setter.call(password, ${JSON.stringify(account.password)}); password.dispatchEvent(new Event('input', {bubbles: true}));
    login.form.querySelector('button[type="submit"]').click(); return true;`));
  await page.wait(inspect('return visible(document.querySelector(".o_main_navbar")) && !document.querySelector("input[name=password]");'), 45000);
  const actual = await page.evaluate(inspect(`const label = document.querySelector('.o_user_menu .oe_topbar_name');
    return {user_label: label ? [...label.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim() : '',
      password_field_count: document.querySelectorAll('input[name="password"], input[type="password"]').length};`));
  return {rendered_form: true, submitted: true, authenticated: true, ...actual};
}

async function collectValidatedList(page, expression, key, inputs, role, {timeoutMs = 30000, intervalMs = 100} = {}) {
  requireEvidence(Number.isInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 30000 &&
    Number.isInteger(intervalMs) && intervalMs >= 0 && intervalMs <= 1000);
  const deadline = Date.now() + timeoutMs;
  let lastDiagnostics;
  while (Date.now() <= deadline) {
    try {
      const records = await page.evaluate(expression);
      if (records?.diagnostics) lastDiagnostics = sanitizeDiagnostics(records.diagnostics);
      const classified = classifyListRows(role === 'other_sales' || records.has_native_sample_data ? records.unfiltered_record_names : records.record_names,
        records.has_native_sample_data, records.empty_state, role, inputs);
      return validateListEvidence(key, {...records, ...classified, model: records.model || MENU_MODELS[key],
        record_name_visibility: classified.has_native_sample_data ? [] : records.record_name_visibility}, inputs, role);
    } catch {
      if (Date.now() >= deadline) break;
      await delay(intervalMs);
    }
  }
  const error = new Error('The rendered target list did not satisfy acceptance before its deadline.');
  if (lastDiagnostics) error.diagnostics = lastDiagnostics;
  throw error;
}

async function menuPage(page, key, menu, options, inputs, role, progress) {
  progress.expectedRecordName = undefined;
  progress.stage = `custom ${key} rendered list readiness`;
  await page.navigate(`${options.baseUrl}/odoo/action-${menu.action_id}?debug=1`);
  const ready = inspect('return visible(document.querySelector(".o_list_view")) || visible(document.querySelector(".o_action_manager .o_view_nocontent .o_nocontent_help"));');
  await page.wait(ready);
  const selector = `[data-menu-xmlid="portfolio_trade.menu_portfolio_${key}"]`;
  progress.stage = `custom ${key} visible menu`;
  await page.wait(inspect(`return [...document.querySelectorAll(${JSON.stringify(selector)})].some(visible);`));
  progress.stage = `custom ${key} clicked menu readiness`;
  await page.evaluate(inspect(`const item = [...document.querySelectorAll(${JSON.stringify(selector)})].find(visible); item.click(); return true;`));
  await page.wait(ready);
  progress.stage = `custom ${key} collected native evidence`;
  return collectValidatedList(page, inspect(`const list = document.querySelector('.o_list_view');
    const form = document.querySelector('.o_form_view'); const nameWidget = form && field(form, 'name');
    const empty = document.querySelector('.o_action_manager .o_view_nocontent .o_nocontent_help');
    const root = visible(list) ? list : empty?.closest('.o_action') || empty;
    const nameCells = [...(root?.querySelectorAll('tr.o_data_row td[name="name"]') || [])];
    const headers = [...(root?.querySelectorAll('th[data-name]') || [])];
    return {visible: visible(root), menu_label: text([...document.querySelectorAll(${JSON.stringify(selector)})].find(visible)),
      record_names: nameCells.filter(visible).map(text), record_name_visibility: nameCells.filter(visible).map(visible),
      unfiltered_record_names: nameCells.map(text), columns: headers.filter(visible).map(el => el.dataset.name),
      column_visibility: Object.fromEntries(headers.map(el => [el.dataset.name, visible(el)])),
      has_native_sample_data: !!list?.querySelector('.o_list_table')?.closest('.o_view_sample_data'),
      empty_state: {visible: visible(empty), selector: '.o_view_nocontent .o_nocontent_help', text: text(empty)},
      diagnostics: {document_ready: document.readyState === 'complete',
        navbar_present: !!document.querySelector('.o_main_navbar'), navbar_visible: visible(document.querySelector('.o_main_navbar')),
        list_present: !!list, list_visible: visible(list), table_present: !!list?.querySelector('.o_list_table'),
        table_visible: visible(list?.querySelector('.o_list_table')), row_count: nameCells.length,
        empty_helper_present: !!empty, empty_helper_visible: visible(empty), empty_help_text_length: text(empty).length,
        form_present: !!form, form_visible: visible(form), name_widget_present: !!nameWidget,
        name_widget_visible: visible(nameWidget), expected_name_matches: false,
        error_dialog_present: !!document.querySelector('.o_error_dialog'),
        native_columns: headers.map(el => el.dataset.name), visible_native_columns: headers.filter(visible).map(el => el.dataset.name),
        view_classes: [...document.querySelectorAll('.o_action_manager, .o_action_manager [class], .o_error_dialog')].flatMap(el => [...el.classList]),
        visible_menu_xmlids: [...document.querySelectorAll('[data-menu-xmlid]')].filter(visible).map(el => el.dataset.menuXmlid)}};`),
  key, inputs, role);
}

async function failureDiagnostics(page, expectedRecordName) {
  const raw = await page.evaluate(inspect(`const list = document.querySelector('.o_list_view');
    const form = document.querySelector('.o_form_view'); const nameWidget = form && field(form, 'name');
    const navbar = document.querySelector('.o_main_navbar');
    const table = list?.querySelector('.o_list_table');
    const empty = document.querySelector('.o_action_manager .o_view_nocontent .o_nocontent_help');
    return {document_ready: document.readyState === 'complete', navbar_present: !!navbar, navbar_visible: visible(navbar),
      list_present: !!list, list_visible: visible(list), table_present: !!table, table_visible: visible(table),
      row_count: document.querySelectorAll('.o_action_manager tr.o_data_row').length,
      empty_helper_present: !!empty, empty_helper_visible: visible(empty), empty_help_text_length: (empty?.textContent || '').trim().length,
      form_present: !!form, form_visible: visible(form), name_widget_present: !!nameWidget, name_widget_visible: visible(nameWidget),
      expected_name_matches: ${JSON.stringify(expectedRecordName || null)} !== null && text(nameWidget) === ${JSON.stringify(expectedRecordName || null)},
      error_dialog_present: !!document.querySelector('.o_error_dialog'),
      view_classes: [...document.querySelectorAll('.o_action_manager, .o_action_manager [class], .o_error_dialog')].flatMap(el => [...el.classList]),
      native_columns: [...document.querySelectorAll('.o_action_manager th[data-name]')].map(el => el.dataset.name),
      visible_native_columns: [...document.querySelectorAll('.o_action_manager th[data-name]')].filter(visible).map(el => el.dataset.name),
      visible_menu_xmlids: [...document.querySelectorAll('[data-menu-xmlid]')].filter(visible).map(el => el.dataset.menuXmlid)};`));
  return sanitizeDiagnostics(raw);
}

async function clickRecord(page, name, progress, kind) {
  requireEvidence(['invoice', 'stock'].includes(kind));
  progress.expectedRecordName = name;
  progress.stage = `native ${kind} row readiness`;
  progress.stage = `native ${kind} record click`;
  await page.wait(inspect(`const cell = [...document.querySelectorAll('.o_list_view tr.o_data_row td[name="name"]')].find(el => visible(el) && text(el) === ${JSON.stringify(name)});
    if (!cell) return false; cell.click(); return true;`));
  progress.stage = `native ${kind} form readiness`;
  await page.wait(inspect(`const form = document.querySelector('.o_form_view'); return visible(form) && fieldText(form, 'name') === ${JSON.stringify(name)};`));
  progress.stage = `native ${kind} form ready`;
}

async function sourceAndTrace(page, role, options, inputs, state) {
  await page.navigate(options.baseUrl + inputs.roleReport.roles[role].source_route + '?debug=1');
  await page.wait(inspect(`const form = document.querySelector('.o_form_view'); return visible(form) && fieldText(form, 'name') === ${JSON.stringify(inputs.flow.identities.sale_order.name)};`));
  state.source = await page.evaluate(inspect(`const form = document.querySelector('.o_form_view');
    const names = ${JSON.stringify(SALES_BUTTONS)};
    const controls = [...form.querySelectorAll('button[name]')].filter(el => visible(el) && names.includes(el.name)).map(el => el.name);
    for (const selector of ['.o_form_button_create', '.o_form_button_save', '.o_form_button_edit']) {
      if ([...document.querySelectorAll(selector)].some(visible)) controls.push(selector);
    }
    return {visible: visible(form), name: fieldText(form, 'name'), readonly: !!form.querySelector('.o_form_readonly'), write_controls: controls};`));
  await page.wait(inspect(`return [...document.querySelectorAll('button[name="action_portfolio_trace"]')].some(visible);`));
  await page.evaluate(inspect(`[...document.querySelectorAll('button[name="action_portfolio_trace"]')].find(visible).click(); return true;`));
  await page.wait(inspect('return [...document.querySelectorAll(".modal .o_form_view")].some(form => visible(field(form, "gross_delivered_qty")));'));
  const trace = await page.evaluate(inspect(`const form = [...document.querySelectorAll('.modal .o_form_view')].find(el => visible(field(el, 'gross_delivered_qty')));
    const quantities = Object.fromEntries(${JSON.stringify(Object.keys(PHYSICAL))}.map(name => [name, fieldText(form, name)]));
    const quantity_visibility = Object.fromEntries(${JSON.stringify(Object.keys(PHYSICAL))}.map(name => [name, visible(field(form, name))]));
    const line_rows = [...field(form, 'line_ids').querySelectorAll('tr.o_data_row')].filter(visible).map(row => ({
      visible: visible(row), product_visible: visible(row.querySelector('td[name="product_id"]')),
      unit_visible: visible(row.querySelector('td[name="product_uom_id"]')),
      product: text(row.querySelector('td[name="product_id"]')), unit: text(row.querySelector('td[name="product_uom_id"]')),
      quantity_visibility: Object.fromEntries(${JSON.stringify(Object.keys(PHYSICAL))}.map(name => [name, visible(row.querySelector('td[name="' + name + '"]'))])),
      ...Object.fromEntries(${JSON.stringify(Object.keys(PHYSICAL))}.map(name => [name, text(row.querySelector('td[name="' + name + '"]'))]))}));
    return {opened_by_click: true, visible: visible(form), source_name: fieldText(form, 'sale_order_id'), quantities, quantity_visibility, line_rows,
      linked_document_names: [], financial_field_names: [], financial_values: {}};`));
  const documentNames = new Set(), financialNames = new Set();
  const tabs = ['Purchases and receipts', 'Deliveries and backorders', 'Returns', ...(role === 'finance' ? ['Invoices and credits'] : [])];
  for (const label of tabs) {
    await page.evaluate(inspect(`const tab = [...document.querySelectorAll('.modal .o_notebook .nav-link')].find(el => text(el) === ${JSON.stringify(label)});
      if (!tab) throw new Error('Trace tab unavailable'); tab.click(); return true;`));
    await page.wait(inspect(`const tab = [...document.querySelectorAll('.modal .o_notebook .nav-link')].find(el => text(el) === ${JSON.stringify(label)}); return tab?.classList.contains('active');`));
    const stage = await page.evaluate(inspect(`const form = document.querySelector('.modal .o_form_view');
      const fields = ['purchase_order_ids', 'receipt_picking_ids', 'delivery_picking_ids', 'backorder_picking_ids', 'return_picking_ids', 'invoice_ids', 'credit_note_ids'];
      return {names: fields.flatMap(name => [...(field(form, name)?.querySelectorAll('tr.o_data_row td[name="name"]') || [])].filter(visible).map(text)),
        financial: [...new Set([...form.querySelectorAll('[name], [data-name]')].map(el => el.getAttribute('name') || el.dataset.name).filter(name => ${JSON.stringify(FINANCIAL)}.includes(name)))],
        values: Object.fromEntries(['posted_invoice_qty', 'posted_credit_qty', 'posted_signed_amount'].filter(name => visible(field(form, name))).map(name => [name, fieldText(form, name)]))};`));
    stage.names.forEach(name => documentNames.add(name));
    stage.financial.forEach(name => financialNames.add(name));
    Object.assign(trace.financial_values, stage.values);
  }
  trace.linked_document_names = [...documentNames];
  trace.financial_field_names = [...financialNames];
  state.trace = trace;
}

async function checkRole(cdp, role, options, inputs, progress) {
  const context = await cdp.call('Target.createBrowserContext', {disposeOnDetach: true});
  let page;
  try {
    const target = await cdp.call('Target.createTarget', {url: 'about:blank', browserContextId: context.browserContextId});
    const attached = await cdp.call('Target.attachToTarget', {targetId: target.targetId, flatten: true});
    page = new Page(cdp, attached.sessionId);
    await page.call('Page.enable');
    await page.call('Runtime.enable');
    await page.call('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1050, deviceScaleFactor: 1, mobile: false});
    progress.stage = 'rendered login';
    const state = {role, login: await login(page, role, options, inputs), source: null, trace: null,
      invoice: null, screenshot: `${role}.png`};
    progress.stage = 'custom menu and native records';
    let primary;
    const labels = [];
    for (const key of ROLE_MENUS[role]) {
      const result = await menuPage(page, key, inputs.roleReport.roles[role].menus[key], options, inputs, role, progress);
      labels.push(result.menu_label);
      if (!primary) primary = result;
      if (key === 'invoices') {
        await clickRecord(page, inputs.flow.identities.invoice_first.name, progress, 'invoice');
        state.invoice = {visible: true, model: 'account.move', name: inputs.flow.identities.invoice_first.name};
      }
    }
    state.page = {visible: primary.visible, model: role === 'warehouse' ? 'stock.picking' : role === 'buyer' ? 'purchase.order' : 'sale.order',
      menu_labels: labels, record_names: primary.record_names, record_name_visibility: primary.record_name_visibility,
      empty_state: primary.empty_state,
      has_native_sample_data: primary.has_native_sample_data, sample_row_count: primary.sample_row_count,
      foreign_source_absent: role === 'other_sales' && !Object.values(inputs.flow.identities).some(item => primary.record_names.includes(item.name))};
    if (!['warehouse', 'other_sales'].includes(role)) {
      progress.stage = 'rendered source and clicked fulfillment trace';
      await sourceAndTrace(page, role, options, inputs, state);
    } else if (role === 'warehouse') {
      await clickRecord(page, inputs.flow.identities.delivery_first.name, progress, 'stock');
      requireEvidence(await page.evaluate(inspect('return !document.querySelector("button[name=action_portfolio_trace]");')));
    }
    progress.stage = 'safe screenshot';
    requireEvidence(await page.evaluate('document.querySelectorAll("input[name=password], input[type=password]").length === 0'));
    validateDomEvidence(role, state, inputs);
    const screenshot = await page.call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    safeWrite(path.join(options.outputDir, state.screenshot), Buffer.from(screenshot.data, 'base64'));
    return state;
  } catch (error) {
    if (error.diagnostics) progress.diagnostics = sanitizeDiagnostics(error.diagnostics);
    else if (page) {
      try { progress.diagnostics = await failureDiagnostics(page, progress.expectedRecordName); } catch { /* Keep the controlled stage failure. */ }
    }
    throw error;
  } finally {
    await cdp.call('Target.disposeBrowserContext', {browserContextId: context.browserContextId}).catch(() => {});
  }
}

function resolveChrome(explicit) {
  const candidates = explicit ? [path.resolve(ROOT, explicit)] : [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ...(process.env.PATH || '').split(path.delimiter).flatMap(directory =>
      ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'].map(name => path.join(directory, name)))];
  for (const candidate of candidates) {
    try { fs.accessSync(candidate, fs.constants.X_OK); if (fs.statSync(candidate).isFile()) return candidate; } catch { /* Next installed binary. */ }
  }
  throw new Error('An existing Chrome binary is required.');
}

function safeWrite(filename, data) {
  safeRuntimePath(filename, false);
  const descriptor = fs.openSync(filename, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW, 0o644);
  try { fs.writeFileSync(descriptor, data); } finally { fs.closeSync(descriptor); }
}

function createChromeLaunch(args, parentEnvironment = process.env) {
  // Chromium's Linux singleton uses OS TMPDIR for Unix IPC; profile/cache arguments stay project-local.
  const directory = fs.mkdtempSync(path.join(fs.realpathSync('/tmp'), 'odoo-cdp-'));
  fs.chmodSync(directory, 0o700);
  const owned = fs.lstatSync(directory);
  return {args: [...args], env: {...parentEnvironment, TMPDIR: directory}, ipcDirectory: directory,
    cleanup(child) {
      requireEvidence(!child || child.exitCode !== null || child.signalCode !== null);
      let current;
      try { current = fs.lstatSync(directory); } catch (error) { if (error.code === 'ENOENT') return; throw error; }
      requireEvidence(current.isDirectory() && !current.isSymbolicLink() && current.uid === process.getuid() &&
        current.dev === owned.dev && current.ino === owned.ino);
      fs.rmSync(directory, {recursive: true, force: false});
    }};
}

async function main(argv = process.argv.slice(2)) {
  const progress = {role: 'input', stage: 'validation'};
  let options, profile, chrome, cdp, startup, launch;
  try {
    options = parseArgs(argv);
    const inputs = readInputs(options);
    progress.stage = 'existing Chrome resolution';
    const binary = resolveChrome(options.chromePath);
    requireEvidence(typeof global.WebSocket === 'function');
    fs.mkdirSync(options.outputDir, {recursive: true});
    profile = fs.mkdtempSync(path.join(options.outputDir, '.chrome-'));
    progress.stage = 'existing Chrome startup';
    launch = createChromeLaunch(['--headless=new', '--no-first-run', '--no-default-browser-check',
      '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
      '--disable-default-apps', '--metrics-recording-only', '--no-pings', '--no-sandbox',
      '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
      `--user-data-dir=${profile}`, `--disk-cache-dir=${path.join(profile, 'cache')}`, 'about:blank']);
    startup = {binary, started_at: Date.now(), stderr: Buffer.alloc(0)};
    chrome = childProcess.spawn(binary, launch.args, {env: launch.env, stdio: ['ignore', 'ignore', 'pipe']});
    chrome.stderr.on('data', chunk => {
      const remaining = 8192 - startup.stderr.length;
      if (remaining > 0) startup.stderr = Buffer.concat([startup.stderr, chunk.subarray(0, remaining)]);
    });
    let failed = false;
    chrome.on('error', error => { failed = true; startup.spawn_code = error.code; });
    const deadline = Date.now() + 15000;
    const portFile = path.join(profile, 'DevToolsActivePort');
    startup.port_file = portFile;
    while (!fs.existsSync(portFile) && Date.now() < deadline && !failed && chrome.exitCode === null) await delay(100);
    startup.timed_out = Date.now() >= deadline && !fs.existsSync(portFile);
    requireEvidence(!failed && fs.existsSync(portFile));
    const [port, route] = fs.readFileSync(portFile, 'utf8').trim().split('\n');
    requireEvidence(/^\d+$/.test(port) && /^\/devtools\/browser\/[\w-]+$/.test(route));
    cdp = await Cdp.connect(`ws://127.0.0.1:${port}${route}`);
    const evidence = {};
    for (const role of ROLES) {
      progress.role = role;
      evidence[role] = await checkRole(cdp, role, options, inputs, progress);
    }
    const report = validateBrowserReport(evidence, inputs);
    safeWrite(path.join(options.outputDir, 'report.json'), JSON.stringify(report, null, 2) + '\n');
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
    return 0;
  } catch {
    const startupSafe = startup && progress.stage === 'existing Chrome startup' ? chromeStartupDiagnostics({
      stderr: startup.stderr.toString('utf8'), binary: startup.binary, spawn_code: startup.spawn_code,
      exit_code: chrome?.exitCode, signal: chrome?.signalCode, duration_ms: Date.now() - startup.started_at,
      port_file_present: !!startup.port_file && fs.existsSync(startup.port_file),
      tmpdir_exists: !!launch?.env.TMPDIR && fs.existsSync(launch.env.TMPDIR), timed_out: startup.timed_out,
      debugging_loopback: chrome?.spawnargs.includes('--remote-debugging-address=127.0.0.1'),
    }) : undefined;
    const report = {ok: false, role: progress.role, stage: progress.stage,
      reason: progress.stage.startsWith('existing Chrome') ? 'An existing Chrome binary is required and must start successfully.' : 'Local rendered-browser verification failed.',
      ...(startupSafe ? {startup_diagnostics: startupSafe} : {}),
      ...(progress.diagnostics ? {diagnostics: sanitizeDiagnostics(progress.diagnostics)} : {})};
    if (profile && options) {
      try { safeWrite(path.join(options.outputDir, 'report.json'), JSON.stringify(report, null, 2) + '\n'); } catch { /* Preserve the safe stdout failure. */ }
    }
    process.stdout.write(JSON.stringify(report) + '\n');
    return 1;
  } finally {
    if (cdp) cdp.close();
    if (chrome && chrome.exitCode === null && chrome.signalCode === null && !chrome.killed) {
      chrome.kill('SIGTERM');
      const deadline = Date.now() + 5000;
      while (chrome.exitCode === null && chrome.signalCode === null && Date.now() < deadline) await delay(100);
      if (chrome.exitCode === null && chrome.signalCode === null) {
        chrome.kill('SIGKILL');
        await new Promise(resolve => { chrome.once('exit', resolve); setTimeout(resolve, 3000); });
      }
    }
    if (launch) launch.cleanup(chrome);
    if (profile) fs.rmSync(profile, {recursive: true, force: true});
  }
}

module.exports = {parseArgs, readInputs, validateRoleReport, classifyListRows, collectValidatedList, clickRecord, validateListEvidence, validateDomEvidence,
  validateBrowserReport, sanitizeDiagnostics, chromeStartupDiagnostics, createChromeLaunch, Cdp, Page, resolveChrome, main};
if (require.main === module) main().then(status => { process.exitCode = status; });
