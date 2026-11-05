'use strict';

// Node 22, rendered DOM, and the installed Chrome binary. No business RPC client.
const fs = require('node:fs');
const path = require('node:path');
const {isDeepStrictEqual: equal} = require('node:util');
const childProcess = require('node:child_process');
const {Cdp, Page, resolveChrome, createChromeLaunch, chromeStartupDiagnostics} = require('../scripts/browser_m2c_check.js');
const ROOT = path.resolve(__dirname, '..');
const ROLES = ['settings', 'operator', 'integration', 'sales', 'other_sales'];
const OLD = ['sales', 'manager', 'manager2', 'buyer', 'warehouse', 'finance', 'other_sales'];
const SOURCE = 'portfolio.import.source', JOB = 'portfolio.import.job', EVENT = 'portfolio.import.attempt';
const MODELS = {sources: SOURCE, customer_maps: 'portfolio.import.customer.map', product_maps: 'portfolio.import.product.map',
  jobs: JOB, attempts: EVENT, sales: 'sale.order'};
const MENUS = {settings: ['sources', 'customer_maps', 'product_maps'], operator: ['jobs', 'attempts', 'sales'],
  integration: ['jobs', 'attempts'], sales: ['sales'], other_sales: ['sales']};
const ROOT_MENUS = {trade: 'portfolio_trade.menu_portfolio_root', import: 'portfolio_order_bridge.menu_import_root', config: 'portfolio_order_bridge.menu_import_config'};
const PAGES = {settings: ['source', 'customer_maps', 'product_maps', 'cron'],
  operator: ['job_success', 'job_repaired', 'attempts', 'audit_success', 'audit_repaired', 'quote_success', 'quote_repaired'],
  integration: ['job_success', 'job_repaired', 'attempts', 'audit_success', 'audit_repaired'],
  sales: ['quote_success', 'quote_repaired'], other_sales: ['sales']};
const COMMON = ['session', 'groups', 'menus', 'actions', 'views', 'healthy_after_negatives', 'business_unchanged',
  'cross_company', 'private_rpc_denied', 'no_scope_spoof', 'audit_crud_denied', 'scoped_queries'];
const CHECKS = {settings: ['service_denied', 'job_acl_denied', 'cron_configuration', 'source_catalog'],
  operator: ['source_private_fields_denied', 'queue_empty', 'done_retry_denied', 'quote_details'],
  integration: ['service_denied', 'native_so_denied', 'mappings_denied', 'source_private_fields_denied', 'idempotent_reception'],
  sales: ['service_denied', 'job_acl_denied', 'approval_guard', 'quote_details'],
  other_sales: ['service_denied', 'job_acl_denied', 'foreign_quotes_hidden']};
const GROUPS = ['base.group_user', 'base.group_system', 'base.group_erp_manager',
  'portfolio_order_bridge.group_import_operator', 'portfolio_order_bridge.group_import_integration',
  'sales_team.group_sale_salesman', 'sales_team.group_sale_salesman_all_leads', 'sales_team.group_sale_manager',
  'purchase.group_purchase_user', 'stock.group_stock_user', 'account.group_account_invoice'];
const M1 = {company_a: 'res.company', company_b: 'res.company', warehouse_a: 'stock.warehouse', customer: 'res.partner',
  vendor: 'res.partner', initial_bearing_move: 'stock.move', ...Object.fromEntries(OLD.map(r => [`user_${r}`, 'res.users'])),
  ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(r => [`product_${r}`, 'product.product'])),
  ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(r => [`supplier_${r}`, 'product.supplierinfo']))};
const STATIC = {...Object.fromEntries(ROLES.slice(0, 3).map(r => [`user_${r}`, 'res.users'])), customer: 'res.partner',
  vendor: 'res.partner', ...Object.fromEntries([1, 2].flatMap(i => [[`product_item${i}`, 'product.product'],
    [`template_item${i}`, 'product.template'], [`supplier_item${i}`, 'product.supplierinfo']])), source: SOURCE,
  customer_map: MODELS.customer_maps, product_map_item1: MODELS.product_maps, product_map_item2: MODELS.product_maps,
  processing_cron: 'ir.cron', processing_server_action: 'ir.actions.server'};
const DYNAMIC = {job_success: JOB, quote_success: 'sale.order', attempt_success: EVENT, job_repaired: JOB,
  quote_repaired: 'sale.order', attempt_failed: EVENT, attempt_retry: EVENT, attempt_repaired: EVENT, product_map_repair: MODELS.product_maps};
const EVENTS = [['attempt_success', 'success', 'done', 1], ['attempt_failed', 'repaired', 'failed', 1],
  ['attempt_retry', 'repaired', 'retry', 1], ['attempt_repaired', 'repaired', 'done', 2]];
const LABEL = {source: 'Portfolio bridge acceptance source', customer: 'Portfolio bridge acceptance customer',
  operator: 'Portfolio bridge acceptance operator', integration: 'Portfolio bridge acceptance integration', sales: 'Acceptance sales', company: 'PARTS-A'};
const REASON = 'Acceptance SKU mapping repaired';
const INPUT_KEYS = ['credentials', 'salesCredentials', 'acceptanceReport', 'profileReport', 'flowReport', 'roleReport'];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function requireEvidence(value) { if (!value) throw new Error('INVALID_EVIDENCE'); }
function keys(value, expected) {
  requireEvidence(value && typeof value === 'object' && !Array.isArray(value) &&
    equal(Object.keys(value).sort(), [...expected].sort()));
}
const id = value => Number.isSafeInteger(value) && value > 0;
const clean = value => typeof value === 'string' && value.length > 0 && value.length <= 200 &&
  value === value.trim() && !/[\x00-\x1f\ud800-\udfff]/u.test(value);
const menuXmlid = key => key === 'sales' ? 'portfolio_trade.menu_portfolio_sales' : `portfolio_order_bridge.menu_import_${key}`;
const external = suffix => `BRIDGE-ACCEPTANCE-${suffix === 'success' ? 'SUCCESS' : 'RETRY'}`;
const expectedMenus = role => [ROOT_MENUS.trade, ...(ROLES.indexOf(role) < 3 ? [ROOT_MENUS.import] : []),
  ...MENUS[role].map(menuXmlid)];
const screenshotName = (role, key) => `${role}-${key}.png`;

function runtimePath(value, {existing = false, directory = false} = {}) {
  requireEvidence(typeof value === 'string' && value && !value.split(/[\\/]/).some(part => part === '..' || part === '.'));
  const result = path.resolve(ROOT, value), roots = [path.join(ROOT, '.runtime')];
  if (path.basename(path.dirname(ROOT)) === '.worktrees') {
    const main = path.dirname(path.dirname(ROOT));
    const marker = path.join(ROOT, '.git');
    if (fs.existsSync(marker) && fs.lstatSync(marker).isFile()) {
      const text = fs.readFileSync(marker, 'utf8').trim();
      if (text.startsWith(`gitdir: ${path.join(main, '.git', 'worktrees')}${path.sep}`)) roots.push(path.join(main, '.runtime'));
    }
  }
  requireEvidence(roots.some(root => { const rel = path.relative(root, result); return rel && !rel.startsWith('..') && !path.isAbsolute(rel); }));
  for (let current = result; ; current = path.dirname(current)) {
    let info;
    try { info = fs.lstatSync(current); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (info) requireEvidence(!info.isSymbolicLink() && (current === result || info.isDirectory()));
    if (current === path.dirname(current)) break;
  }
  if (fs.existsSync(result)) {
    const info = fs.lstatSync(result);
    requireEvidence(info.uid === process.getuid() && (directory ? info.isDirectory() : info.isFile()));
  } else requireEvidence(!existing);
  return result;
}
function validateTarget(baseUrl, database) {
  requireEvidence(typeof baseUrl === 'string' && /^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(baseUrl));
  const url = new URL(baseUrl);
  requireEvidence(Number(url.port) > 0 && Number(url.port) < 65536 && ['odoo_portfolio', 'odoo_portfolio_m1_test'].includes(database));
}
function parseArgs(argv) {
  const names = {'--base-url': 'baseUrl', '--database': 'database', '--credentials': 'credentials',
    '--sales-credentials': 'salesCredentials', '--acceptance-report': 'acceptanceReport', '--profile-report': 'profileReport',
    '--flow-report': 'flowReport', '--role-report': 'roleReport', '--output-dir': 'outputDir', '--chrome-path': 'chromePath'};
  requireEvidence(Array.isArray(argv) && argv.length % 2 === 0);
  const result = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = names[argv[i]];
    requireEvidence(key && !(key in result) && typeof argv[i + 1] === 'string' && argv[i + 1] && !argv[i + 1].startsWith('--'));
    result[key] = argv[i + 1];
  }
  for (const key of ['baseUrl', 'database', ...INPUT_KEYS, 'outputDir']) requireEvidence(typeof result[key] === 'string');
  validateTarget(result.baseUrl, result.database);
  for (const key of INPUT_KEYS) result[key] = runtimePath(result[key]);
  result.outputDir = runtimePath(result.outputDir, {directory: true});
  return result;
}
// Detect duplicate keys before JSON.parse can silently replace them, at every depth.
function parseJson(raw) {
  requireEvidence(Buffer.byteLength(raw, 'utf8') <= 8 * 1024 * 1024);
  let i = 0, depth = 0;
  const ws = () => { while (/\s/.test(raw[i] || '') && i < raw.length) i++; };
  const string = () => {
    const start = i++; while (i < raw.length) { if (raw[i++] === '"') return JSON.parse(raw.slice(start, i)); if (raw[i - 1] === '\\') i++; }
    throw new Error('INVALID_INPUT');
  };
  const value = () => {
    ws(); requireEvidence(++depth <= 100);
    if (raw[i] === '{') {
      i++; ws(); const seen = new Set();
      if (raw[i] !== '}') for (;;) {
        requireEvidence(raw[i] === '"'); const key = string(); requireEvidence(!seen.has(key)); seen.add(key);
        ws(); requireEvidence(raw[i++] === ':'); value(); ws(); if (raw[i] !== ',') break; i++; ws();
      }
      requireEvidence(raw[i++] === '}');
    } else if (raw[i] === '[') {
      i++; ws(); if (raw[i] !== ']') for (;;) { value(); ws(); if (raw[i] !== ',') break; i++; }
      requireEvidence(raw[i++] === ']');
    } else if (raw[i] === '"') string();
    else { const start = i; while (i < raw.length && !/[\s,\]}]/.test(raw[i])) i++; requireEvidence(i > start); JSON.parse(raw.slice(start, i)); }
    depth--;
  };
  value(); ws(); requireEvidence(i === raw.length); return JSON.parse(raw);
}
function readJson(filename, privateFile = false) {
  const descriptor = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const info = fs.fstatSync(descriptor);
    requireEvidence(info.isFile() && info.uid === process.getuid() && info.size <= (privateFile ? 65536 : 8 * 1024 * 1024) &&
      (!privateFile || (info.mode & 0o777) === 0o600));
    return parseJson(fs.readFileSync(descriptor, 'utf8'));
  } finally { fs.closeSync(descriptor); }
}
function credentials(filename, database, roles, prefix) {
  const result = readJson(filename, true); keys(result, ['schema_version', 'database', 'accounts']);
  requireEvidence(result.schema_version === 1 && result.database === database); keys(result.accounts, roles);
  const secrets = new Set();
  for (const role of roles) {
    const account = result.accounts[role]; keys(account, ['login', 'password']);
    requireEvidence(account.login === `${prefix}${role}` && typeof account.password === 'string' && account.password.length >= 24 &&
      account.password.trim() && !secrets.has(account.password)); secrets.add(account.password);
  }
  return result;
}
function validateIdentities(value, models) {
  keys(value, Object.keys(models));
  for (const [key, model] of Object.entries(models)) { keys(value[key], ['model', 'id']); requireEvidence(value[key].model === model && id(value[key].id)); }
}
function validateSourceReports(inputs) {
  const {acceptance: acceptance, profile, flow, database} = inputs;
  keys(acceptance, ['namespace', 'identities', 'record_counts', 'bearing_quantity', 'bearing_move_count', 'initial_bearing_move_id']);
  validateIdentities(acceptance.identities, M1);
  const old = acceptance.identities;
  requireEvidence(acceptance.namespace === 'portfolio_trade_acceptance' && equal(acceptance.record_counts,
    Object.values(M1).reduce((all, model) => ({...all, [model]: (all[model] || 0) + 1}), {})) &&
    acceptance.bearing_quantity === 40 && acceptance.bearing_move_count === 1 && acceptance.initial_bearing_move_id === old.initial_bearing_move.id);
  keys(profile, ['schema_version', 'database', 'namespace', 'company_id', 'source_id', 'identities', 'cron_active', 'borrowed_m1_identities', 'borrowed_finance_pricelist_id']);
  keys(flow, ['schema_version', 'database', 'namespace', 'company_id', 'source_id', 'identities', 'jobs', 'quotes', 'events', 'm1_preservation']);
  validateIdentities(profile.identities, STATIC); validateIdentities(flow.identities, DYNAMIC);
  const statics = profile.identities, dynamic = flow.identities;
  for (const [report, namespace] of [[profile, 'portfolio_order_bridge_acceptance'], [flow, 'portfolio_order_bridge_flow']]) {
    requireEvidence(report.schema_version === 1 && report.database === database && report.namespace === namespace &&
      report.company_id === old.company_a.id && report.source_id === statics.source.id);
  }
  requireEvidence(profile.cron_active === false && equal(profile.borrowed_m1_identities, old) && id(profile.borrowed_finance_pricelist_id));
  const all = [...Object.values(old), ...Object.values(statics), ...Object.values(dynamic)];
  requireEvidence(new Set(all.map(item => `${item.model}:${item.id}`)).size === all.length);
  keys(flow.jobs, ['success', 'repaired']); keys(flow.quotes, ['success', 'repaired']);
  for (const [suffix, amount, count] of [['success', 309, 1], ['repaired', 161.5, 2]]) {
    requireEvidence(equal(flow.jobs[suffix], {id: dynamic[`job_${suffix}`].id, state: 'done', attempt_count: count,
      submitted_by_id: statics.user_integration.id, processed_by_id: statics.user_operator.id}));
    const quote = flow.quotes[suffix]; keys(quote, ['id', 'name', 'state', 'currency', 'amount_untaxed', 'amount_total', 'owner_id', 'created_by_id', 'approval_required']);
    requireEvidence(quote.id === dynamic[`quote_${suffix}`].id && clean(quote.name) && quote.state === 'draft' && quote.currency === 'CNY' &&
      quote.amount_untaxed === amount && quote.amount_total === amount && quote.owner_id === old.user_sales.id &&
      quote.created_by_id === statics.user_operator.id && quote.approval_required === true);
  }
  requireEvidence(flow.quotes.success.name !== flow.quotes.repaired.name);
  keys(flow.events, EVENTS.map(item => item[0]));
  for (const [key, suffix, kind, no] of EVENTS) requireEvidence(equal(flow.events[key], {id: dynamic[key].id,
    job_id: dynamic[`job_${suffix}`].id, event_type: kind, attempt_no: no, actor_id: statics.user_operator.id, retry_reason: kind === 'retry' ? REASON : null}));
  const preserved = flow.m1_preservation; keys(preserved, ['fingerprint', 'borrowed_m1_identities', 'bearing_stock', 'bearing_move_ids']);
  requireEvidence(typeof preserved.fingerprint === 'string' && /^[0-9a-f]{64}$/.test(preserved.fingerprint) &&
    equal(preserved.borrowed_m1_identities, old) && preserved.bearing_stock === 10 && Array.isArray(preserved.bearing_move_ids) &&
    preserved.bearing_move_ids.length > 0 && preserved.bearing_move_ids.every(id) && new Set(preserved.bearing_move_ids).size === preserved.bearing_move_ids.length);
  return inputs;
}
function expectedGroups(role) {
  const active = ['base.group_user', ...({settings: ['base.group_system', 'base.group_erp_manager'],
    operator: ['portfolio_order_bridge.group_import_operator', 'sales_team.group_sale_salesman'],
    integration: ['portfolio_order_bridge.group_import_integration'], sales: ['sales_team.group_sale_salesman'], other_sales: ['sales_team.group_sale_salesman']}[role])];
  return Object.fromEntries(GROUPS.map(group => [group, active.includes(group)]));
}
function validateRoleReport(report, inputs) {
  validateSourceReports(inputs);
  keys(report, ['schema_version', 'ok', 'database', 'profile_source_id', 'roles', 'business_facts']);
  requireEvidence(report.schema_version === 1 && report.ok === true && report.database === inputs.database && report.profile_source_id === inputs.profile.source_id);
  requireEvidence(equal(report.business_facts, {jobs: 2, draft_quotes: 2, attempt_events: 4, quote_totals_cny: [309, 161.5], preserved_m1_identities: 19, bearing_stock: 10}));
  keys(report.roles, ROLES); const old = inputs.acceptance.identities, statics = inputs.profile.identities;
  const seenMenus = {}, seenActions = {};
  for (const role of ROLES) {
    const item = report.roles[role], allowed = MENUS[role];
    keys(item, ['uid', 'company_id', 'allowed_company_ids', 'active_company_ids', 'groups', 'menus', 'actions', 'views', 'menu_roots', 'source_id', 'mapping_ids', 'cron', 'jobs', 'quotes', 'events', 'checks']);
    const company = old[role === 'other_sales' ? 'company_b' : 'company_a'].id;
    requireEvidence(item.uid === (ROLES.indexOf(role) < 3 ? statics : old)[`user_${role}`].id && item.company_id === company &&
      equal(item.allowed_company_ids, [company]) && equal(item.active_company_ids, [company]) && equal(item.groups, expectedGroups(role)));
    keys(item.menus, allowed); keys(item.actions, allowed); keys(item.views, allowed.map(key => MODELS[key]));
    keys(item.menu_roots, role === 'settings' ? ['trade', 'import', 'config'] : ROLES.indexOf(role) < 3 ? ['trade', 'import'] : ['trade']);
    for (const [key, root] of Object.entries(item.menu_roots)) {
      keys(root, ['id', 'parent_id']); requireEvidence(id(root.id) && root.parent_id === (key === 'trade' ? false : item.menu_roots[key === 'config' ? 'import' : 'trade'].id));
      if (seenMenus[`root_${key}`]) requireEvidence(equal(root, seenMenus[`root_${key}`])); else seenMenus[`root_${key}`] = root;
    }
    for (const key of allowed) {
      const menu = item.menus[key], action = item.actions[key]; keys(menu, ['id', 'action_id', 'model']); keys(action, ['id', 'res_model', 'view_mode']);
      requireEvidence(id(menu.id) && id(action.id) && menu.action_id === action.id && menu.model === MODELS[key] && action.res_model === MODELS[key] &&
        typeof action.view_mode === 'string' && ['list', 'form'].every(kind => action.view_mode.split(',').includes(kind)) && equal(item.views[MODELS[key]], {list: true, form: true}));
      if (seenMenus[key]) requireEvidence(equal(seenMenus[key], menu) && equal(seenActions[key], action));
      seenMenus[key] = menu; seenActions[key] = action;
    }
    requireEvidence(item.source_id === (ROLES.indexOf(role) < 3 ? inputs.profile.source_id : null));
    requireEvidence(equal(item.mapping_ids, ['settings', 'operator'].includes(role) ? {customer: [statics.customer_map.id],
      product: [statics.product_map_item1.id, statics.product_map_item2.id, inputs.flow.identities.product_map_repair.id].sort((a, b) => a - b)} : {}));
    requireEvidence(equal(item.cron, role === 'settings' ? {id: statics.processing_cron.id, active: false, processor_uid: statics.user_operator.id, fixed_code: true, source_link: true} : null));
    for (const [section, readers] of [['jobs', ['operator', 'integration']], ['events', ['operator', 'integration']], ['quotes', ['operator', 'sales']]]) {
      requireEvidence(equal(item[section], readers.includes(role) ? inputs.flow[section] : {}));
    }
    keys(item.checks, [...COMMON, ...CHECKS[role]]); requireEvidence(Object.values(item.checks).every(value => value === true));
  }
  const menuIds = Object.entries(seenMenus).map(([, menu]) => menu.id), actionIds = Object.values(seenActions).map(action => action.id);
  requireEvidence(new Set(menuIds).size === menuIds.length && new Set(actionIds).size === actionIds.length);
  return report;
}
function assertNoInputAlias(destination, inputPaths) {
  requireEvidence(!inputPaths.includes(destination));
  if (fs.existsSync(destination)) {
    const target = fs.statSync(destination);
    requireEvidence(!inputPaths.some(filename => { const source = fs.statSync(filename); return source.dev === target.dev && source.ino === target.ino; }));
  }
}
function readInputs(options) {
  validateTarget(options.baseUrl, options.database);
  const paths = INPUT_KEYS.map(key => runtimePath(options[key], {existing: true}));
  requireEvidence(new Set(paths.map(filename => { const info = fs.statSync(filename); return `${info.dev}:${info.ino}`; })).size === paths.length);
  const output = runtimePath(options.outputDir, {directory: true}); assertNoInputAlias(output, paths);
  for (const filename of ['report.json', ...ROLES.flatMap(role => PAGES[role].map(key => screenshotName(role, key)))]) {
    assertNoInputAlias(runtimePath(path.join(output, filename)), paths);
  }
  const inputs = {database: options.database, acceptance: readJson(paths[2]), profile: readJson(paths[3]), flow: readJson(paths[4]), roleReport: readJson(paths[5]), inputPaths: paths};
  validateRoleReport(inputs.roleReport, inputs);
  inputs.credentials = credentials(paths[0], options.database, ROLES.slice(0, 3), 'portfolio.bridge.acceptance.');
  inputs.salesCredentials = credentials(paths[1], options.database, OLD, 'portfolio.acceptance.');
  return inputs;
}
function numeric(value) {
  requireEvidence(typeof value === 'string' && /^\s*[¥￥]?\s*-?[\d,]+(?:\.\d+)?\s*(?:CNY|%)?\s*$/.test(value));
  return Number(value.replace(/[¥￥,%\s]|CNY/g, ''));
}
function emptyState(value) { return value?.visible === true && value.selector === '.o_view_nocontent .o_nocontent_help' && clean(value.text); }
function classifyBusinessRows(names, sample, empty, role, inputs) {
  requireEvidence(Array.isArray(names) && names.every(clean) && typeof sample === 'boolean');
  if (!sample) return {record_names: names, has_native_sample_data: false, sample_row_count: 0};
  requireEvidence(role === 'other_sales' && emptyState(empty) && names.length > 0 && names.length <= 1000 &&
    !Object.values(inputs.flow.quotes).some(quote => names.includes(quote.name)));
  return {record_names: [], has_native_sample_data: true, sample_row_count: names.length};
}
function visibleField(page, name, expected) {
  const field = page.fields?.[name]; requireEvidence(field?.visible === true && (expected === undefined || equal(field.value, expected))); return field;
}
function productDisplay(value, item) {
  const label = `Portfolio bridge acceptance item ${item}`;
  return value === label || value === `[BRIDGE-ACCEPTANCE-${item}] ${label}`;
}
function validateEvents(rows, expected, withJob = false) {
  requireEvidence(Array.isArray(rows) && rows.length === expected.length);
  for (const [key, suffix, kind, no] of expected) {
    const matches = rows.filter(row => row.event_key === key); requireEvidence(matches.length === 1); const row = matches[0];
    requireEvidence(row.visible === true && numeric(row.attempt_no) === no && row.event_type === kind && row.actor === LABEL.operator &&
      row.retry_reason === (kind === 'retry' ? REASON : '') && (!withJob || row.job === external(suffix)));
  }
}
function validateDomEvidence(role, evidence, inputs) {
  requireEvidence(ROLES.includes(role) && evidence.role === role);
  const login = evidence.login;
  requireEvidence(login?.rendered_form === true && login.submitted === true && login.authenticated === true && login.password_field_count === 0 &&
    login.user_label === (ROLES.indexOf(role) < 3 ? `Portfolio bridge acceptance ${role}` : `Acceptance ${role}`));
  requireEvidence(Array.isArray(evidence.menu_xmlids) && equal([...evidence.menu_xmlids].sort(), expectedMenus(role).sort()) && evidence.import_payload_entry_count === 0 && evidence.import_root_visible === (ROLES.indexOf(role) < 3));
  const configHeader = {visible: true, label: 'Import settings / 导入配置', root_xmlid: ROOT_MENUS.import,
    root_open: true, header_has_xmlid: false, child_xmlids: ['sources', 'customer_maps', 'product_maps'].map(menuXmlid)};
  requireEvidence(equal(evidence.config_headers, role === 'settings' ? [configHeader] : []));
  keys(evidence.pages, PAGES[role]);
  const safePages = {};
  for (const [key, page] of Object.entries(evidence.pages)) {
    const model = key === 'source' ? SOURCE : key === 'cron' ? 'ir.cron' : key === 'attempts' ? EVENT :
      key.startsWith('job_') || key.startsWith('audit_') ? JOB : MODELS[key] || 'sale.order';
    requireEvidence(page?.visible === true && page.model === model && page.screenshot === screenshotName(role, key));
    safePages[key] = {model, visible: true, screenshot: screenshotName(role, key)};
  }
  if (role === 'settings') {
    const {source, customer_maps: customer, product_maps: product, cron} = evidence.pages;
    for (const [name, value] of Object.entries({name: LABEL.source, company_id: LABEL.company, company_code: 'PARTS-A', code: 'B2B-ACCEPTANCE',
      sales_user_id: LABEL.sales, processor_user_id: LABEL.operator})) visibleField(source, name, value);
    visibleField(source, 'integration_user_ids', [LABEL.integration]);
    requireEvidence(visibleField(source, 'processing_cron_id', `Order import / 订单导入: ${LABEL.source}`).linked === true);
    requireEvidence(equal(customer.columns.sort(), ['source_id', 'company_id', 'external_ref', 'partner_id', 'active'].sort()) &&
      customer.rows.length === 1 && equal(customer.rows[0], {visible: true, source_id: LABEL.source, company_id: LABEL.company,
        external_ref: 'BRIDGE-ACCEPTANCE-CUSTOMER', partner_id: LABEL.customer, active: true}));
    requireEvidence(equal(product.columns.sort(), ['source_id', 'company_id', 'external_sku', 'product_id', 'active'].sort()) && product.rows.length === 3);
    for (const sku of ['1', '2', 'REPAIR']) requireEvidence(product.rows.filter(row => equal({...row, product_id: undefined}, {visible: true, source_id: LABEL.source,
      company_id: LABEL.company, external_sku: `BRIDGE-ACCEPTANCE-${sku}`, product_id: undefined, active: true}) && productDisplay(row.product_id, sku === '1' ? 1 : 2)).length === 1);
    requireEvidence(cron.opened_by_link === true);
    for (const [name, value] of Object.entries({name: `Order import / 订单导入: ${LABEL.source}`, active: false, user_id: LABEL.operator, code: 'model._process_pending()'})) visibleField(cron, name, value);
  }
  if (['operator', 'integration'].includes(role)) {
    requireEvidence(equal(evidence.job_list_buttons, role === 'operator' ? ['action_process_pending'] : []));
    for (const suffix of ['success', 'repaired']) {
      const job = evidence.pages[`job_${suffix}`];
      requireEvidence(job.readonly === true && equal(job.write_controls, []) && equal(job.visible_buttons, role === 'operator' ? ['action_open_sale_order'] : []) && job.opened_from_visible_row === true);
      for (const [name, value] of Object.entries({external_order_id: external(suffix), source_id: LABEL.source, company_id: LABEL.company,
        state: 'done', submitted_by_id: LABEL.integration})) visibleField(job, name, value);
      requireEvidence(numeric(visibleField(job, 'attempt_count').value) === inputs.flow.jobs[suffix].attempt_count);
      const quote = visibleField(job, 'sale_order_id', inputs.flow.quotes[suffix].name); requireEvidence(quote.readonly === true && quote.no_open === true);
      requireEvidence(job.metadata?.visible === true && job.metadata.creator === LABEL.integration);
      validateEvents(job.history, EVENTS.filter(([, eventSuffix]) => suffix === eventSuffix));
      const audit = evidence.pages[`audit_${suffix}`]; requireEvidence(audit.readonly === true);
      for (const name of ['raw_payload', 'canonical_payload', 'payload_hash']) {
        const field = visibleField(audit, name); requireEvidence(field.readonly === true && field.nonempty === true && field.value === null);
      }
      Object.assign(safePages[`job_${suffix}`], {external_order_id: external(suffix), state: 'done', attempt_count: inputs.flow.jobs[suffix].attempt_count,
        creator: LABEL.integration, processor: LABEL.operator, history_events: job.history.length});
    }
    requireEvidence(evidence.pages.attempts.readonly === true && equal(evidence.pages.attempts.write_controls, []));
    validateEvents(evidence.pages.attempts.rows, EVENTS, true);
    safePages.attempts.event_count = 4;
  }
  if (['operator', 'sales'].includes(role)) for (const suffix of ['success', 'repaired']) {
    const quote = evidence.pages[`quote_${suffix}`], expected = inputs.flow.quotes[suffix];
    requireEvidence(quote.opened_from_visible_row === true && quote.metadata?.visible === true && quote.metadata.creator === LABEL.operator);
    for (const [name, value] of Object.entries({name: expected.name, partner_id: LABEL.customer, user_id: LABEL.sales,
      state: 'draft', origin: `B2B-ACCEPTANCE:${external(suffix)}`, client_order_ref: `B2B-ACCEPTANCE:${external(suffix)}`, portfolio_approval_state: 'draft'})) visibleField(quote, name, value);
    const totals = visibleField(quote, 'tax_totals'); requireEvidence(totals.readonly === true && totals.widget === 'account-tax-totals-field' && numeric(totals.value) === expected.amount_total);
    requireEvidence(equal(quote.company_scope, {visible: true, label: LABEL.company, origin: 'navbar'}));
    requireEvidence(quote.approval_required?.visible === true && quote.approval_required.value === true &&
      quote.approval_required.submit_button_visible === true && quote.approval_required.approval_tab_visible === true);
    const wanted = suffix === 'success' ? [[2, 3, 35, 0], [1, 2, 120, 15]] : [[1, 1, 120, 15], [2, 2, 35, 15]];
    requireEvidence(Array.isArray(quote.lines) && quote.lines.length === 2);
    for (const [product, quantity, price, discount] of wanted) {
      const matching = quote.lines.filter(line => productDisplay(line.product, product)); requireEvidence(matching.length === 1); const line = matching[0];
      requireEvidence(line.visible === true && line.product_visible === true &&
        ['quantity', 'price_unit', 'subtotal', 'tax'].every(name => line.visibility?.[name] === true) &&
        numeric(line.quantity) === quantity && numeric(line.price_unit) === price && line.tax === '');
      const subtotal = quantity * price * (1 - discount / 100);
      requireEvidence(Math.abs(numeric(line.subtotal) - subtotal) < 0.000001);
      if (line.visibility.discount === true) {
        requireEvidence(numeric(line.discount) === discount && equal(line.discount_evidence, {visible: true, origin: 'native_column', native_column_absent: false}));
      } else {
        requireEvidence(line.visibility.discount === false && line.discount === '' && equal(line.discount_evidence,
          {visible: false, origin: 'visible_quantity_price_subtotal', derived_percent: discount, native_column_absent: true}) &&
          Math.abs((1 - numeric(line.subtotal) / (numeric(line.quantity) * numeric(line.price_unit))) * 100 - discount) < 0.000001);
      }
      if (line.unit_visible === true) requireEvidence(line.unit === 'Units' && equal(line.unit_evidence, {visible: true, origin: 'native_column', native_column_absent: false}));
      else requireEvidence(line.unit_visible === false && line.unit === '' && equal(line.unit_evidence, {visible: false, origin: 'native_feature_disabled', native_column_absent: true}));
    }
    Object.assign(safePages[`quote_${suffix}`], {name: expected.name, state: 'draft', amount_total: expected.amount_total, currency: 'CNY',
      owner: LABEL.sales, creator: LABEL.operator, approval_required: true, product_line_count: 2,
      lines: quote.lines.map(line => ({product: line.product, quantity: numeric(line.quantity), price_unit: numeric(line.price_unit), subtotal: numeric(line.subtotal),
        discount: line.visibility.discount ? {visible: true, percent: numeric(line.discount), origin: 'native_column'} : {...line.discount_evidence},
        unit: {...line.unit_evidence}, tax_empty_visible: true}))});
  }
  if (role === 'other_sales') {
    const page = evidence.pages.sales;
    requireEvidence(equal(page.record_names, []) && page.foreign_quotes_absent === true && emptyState(page.empty_state));
    if (page.has_native_sample_data === true) requireEvidence(Number.isInteger(page.sample_row_count) && page.sample_row_count > 0 && page.sample_row_count <= 1000);
    else requireEvidence(page.has_native_sample_data === false && page.sample_row_count === 0);
    Object.assign(safePages.sales, {real_record_count: 0, sample_row_count: page.sample_row_count});
  }
  return {uid: inputs.roleReport.roles[role].uid, company_id: inputs.roleReport.roles[role].company_id,
    rendered_login: true, menu_xmlids: [...evidence.menu_xmlids], config_headers: evidence.config_headers.map(header => ({...header, child_xmlids: [...header.child_xmlids]})),
    import_payload_entry_count: 0, pages: safePages};
}
function validateBrowserReport(evidence, inputs) {
  validateRoleReport(inputs.roleReport, inputs); keys(evidence, ROLES);
  return {schema_version: 1, ok: true, database: inputs.database, verification: 'automated rendered browser',
    roles: Object.fromEntries(ROLES.map(role => [role, validateDomEvidence(role, evidence[role], inputs)])),
    business_facts: {...inputs.roleReport.business_facts}, workflow: 'completed history inspected without replay'};
}
const STAGES = ['validation', 'chrome_resolution', 'chrome_startup', 'login', 'navigation', 'source', 'customer_maps', 'product_maps', 'cron',
  'job_success', 'job_repaired', 'audit_success', 'audit_repaired', 'attempts', 'quote_success', 'quote_repaired', 'sales', 'metadata', 'screenshot', 'output'];
function safeFailure(progress = {}) {
  return {schema_version: 1, ok: false, role: ROLES.includes(progress.role) ? progress.role : 'input',
    stage: STAGES.includes(progress.stage) ? progress.stage : 'validation', reason: 'RENDERED_BROWSER_FAILED'};
}
function writeOutput(filename, data, inputs) {
  filename = runtimePath(filename); assertNoInputAlias(filename, inputs.inputPaths);
  const temporary = path.join(path.dirname(filename), `.browser-${process.pid}-${require('node:crypto').randomBytes(8).toString('hex')}`);
  const descriptor = fs.openSync(temporary, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW, 0o600);
  try {
    fs.writeFileSync(descriptor, data); fs.fsyncSync(descriptor); fs.closeSync(descriptor);
    runtimePath(filename); assertNoInputAlias(filename, inputs.inputPaths); fs.renameSync(temporary, filename);
  } finally { try { fs.closeSync(descriptor); } catch {} if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}

const DOM = `const geometry = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
const opacity = el => {const value=getComputedStyle(el).opacity;return value===undefined||value===''?1:Number(value);};
const visible = el => {if(!geometry(el))return false;for(let node=el;node;node=node.parentElement){if(opacity(node)<=0||getComputedStyle(node).visibility==='hidden')return false;}return true;};
const settled = el => {if(!visible(el))return false;for(let node=el;node;node=node.parentElement){if(opacity(node)!==1)return false;}return true;};
const text = el => { if (!el) return ''; const input = el.matches?.('input:not([type=checkbox]), textarea') ? el : el.querySelector?.('input:not([type=checkbox]),textarea');
  return (input ? input.value : el.textContent || '').trim(); };
const field = (root, name) => [...(root?.querySelectorAll('.o_field_widget[name="' + name + '"]') || [])].find(visible);
const read = (root, name) => { const el = field(root, name); const checkbox = el?.querySelector('input[type=checkbox]');
  const active = el?.querySelector('[aria-current="step"], [aria-checked="true"], .o_arrow_button_current');
  return {visible: visible(el), value: checkbox ? checkbox.checked : active?.getAttribute('data-value') || text(active || el),
    readonly: !!el && (el.classList.contains('o_readonly_modifier') || !!el.closest('.o_form_readonly')) &&
      ![...(el.querySelectorAll('input,textarea,[contenteditable=true]') || [])].some(node => !node.disabled && !node.readOnly),
    linked: !!el && [...el.querySelectorAll('a,button.o_external_button')].some(visible),
    no_open: !!el && ![...el.querySelectorAll('a,button.o_external_button')].some(visible)}; };
const controls = () => [...document.querySelectorAll('.o_form_button_create,.o_form_button_edit,.o_form_button_save,.o_list_button_add,.o_list_record_remove')].filter(visible).map(el => el.className.split(' ').find(name => /^o_(form_button|list_button|list_record)/.test(name)));
const eventType = value => ({Done:'done',Failed:'failed','Manual retry':'retry'})[value] || value;
const status = value => ({Done:'done',Failed:'failed',Quotation:'draft',Draft:'draft'})[value] || value;
const companyScope = () => { const el = document.querySelector('.o_switch_company_menu .oe_topbar_name'); return {visible:visible(el), label:text(el), origin:'navbar'}; };`;
const inspect = body => `(() => { ${DOM} ${body} })()`;
async function clickVisibleRecord(page, fieldName, expected) {
  requireEvidence(['name', 'external_order_id'].includes(fieldName) && clean(expected));
  await page.wait(inspect(`const cells = [...document.querySelectorAll('.o_list_view:not(.o_view_sample_data) tr.o_data_row td[name="' + ${JSON.stringify(fieldName)} + '"]')]
    .filter(el => visible(el) && !el.closest('.o_view_sample_data') && text(el) === ${JSON.stringify(expected)});
    if(cells.length !== 1) return false; cells[0].click(); return true;`));
}
async function login(page, role, options, inputs) {
  await page.navigate(`${options.baseUrl}/web/login?db=${encodeURIComponent(options.database)}&redirect=${encodeURIComponent('/odoo?debug=1')}`);
  await page.wait(inspect(`return visible(document.querySelector('form.oe_login_form input[name=login]')) && visible(document.querySelector('input[name=password]')) && visible(document.querySelector('form.oe_login_form button[type=submit]'));`));
  const account = (ROLES.indexOf(role) < 3 ? inputs.credentials : inputs.salesCredentials).accounts[role];
  await page.evaluate(inspect(`const username = document.querySelector('form.oe_login_form input[name=login]'); const password = document.querySelector('input[name=password]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    setter.call(username,${JSON.stringify(account.login)}); username.dispatchEvent(new Event('input',{bubbles:true}));
    setter.call(password,${JSON.stringify(account.password)}); password.dispatchEvent(new Event('input',{bubbles:true}));
    username.form.querySelector('button[type=submit]').click(); return true;`));
  await page.wait(inspect(`return visible(document.querySelector('.o_main_navbar')) && !document.querySelector('input[name=password],input[type=password]');`), 45000);
  return page.evaluate(inspect(`const label = document.querySelector('.o_user_menu .oe_topbar_name');
    return {rendered_form:true,submitted:true,authenticated:true,
      user_label:label ? [...label.childNodes].filter(n => n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim() : '',
      password_field_count:document.querySelectorAll('input[name=password],input[type=password]').length};`));
}
async function tab(page, labels) {
  await page.wait(inspect(`const tabs=[...document.querySelectorAll('.o_form_view .o_notebook .nav-link')].filter(visible);
    const chosen=tabs.find(el=>${JSON.stringify(labels)}.some(label=>text(el)===label)); if(!chosen) return false; chosen.click(); return true;`));
  await waitForRenderedPane(page, labels);
}
async function waitForRenderedPane(page, labels) {
  requireEvidence(labels === undefined || (Array.isArray(labels) && labels.length > 0 && labels.every(clean)));
  return page.wait(inspect(`const books=[...document.querySelectorAll('.o_form_view .o_notebook')].filter(geometry);
    const ready=book=>{const selected=[...book.querySelectorAll('.o_notebook_headers .nav-link')].filter(el=>visible(el)&&el.classList.contains('active'));
      const pane=book.querySelector('.o_notebook_content > .tab-pane.active');
      return selected.length===1 && settled(selected[0]) && settled(pane) && (!pane.classList.contains('fade')||pane.classList.contains('show'));};
    const labels=${JSON.stringify(labels || null)};
    if(labels) {const matching=books.filter(book=>[...book.querySelectorAll('.o_notebook_headers .nav-link')]
      .some(el=>visible(el)&&el.classList.contains('active')&&labels.includes(text(el))));return matching.length===1&&ready(matching[0]);}
    return books.every(ready);`));
}
async function captureSettledScreenshot(page) {
  await waitForRenderedPane(page);
  requireEvidence(await page.evaluate(inspect(`return visible(document.querySelector('.o_main_navbar')) && !document.querySelector('input[name=password],input[type=password]') && !document.querySelector('.o_error_dialog');`)));
  return page.call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
}
async function collectVisibleMenus(page) {
  return page.evaluate(inspect(`return [...new Set([...document.querySelectorAll('[data-menu-xmlid]')].filter(visible).map(el=>el.dataset.menuXmlid)
    .filter(value=>value.startsWith('portfolio_trade.menu_portfolio_')||value.startsWith('portfolio_order_bridge.menu_import_')))];`));
}
async function collectImportHeaders(page) {
  return page.evaluate(inspect(`const root=[...document.querySelectorAll('[data-menu-xmlid="portfolio_order_bridge.menu_import_root"]')].find(visible);
    return [...document.querySelectorAll('.dropdown-menu_group.dropdown-header')].filter(el=>visible(el)&&text(el)==='Import settings / 导入配置').map(header=>{
      const children=[]; for(let node=header.nextElementSibling;node;node=node.nextElementSibling) {
        if(node.classList.contains('dropdown-header')) break;
        if(visible(node)&&node.dataset.menuXmlid)children.push(node.dataset.menuXmlid);
      }
      return {visible:visible(header)&&visible(header.closest('.dropdown-menu')),label:text(header),root_xmlid:root?.dataset.menuXmlid||'',
        root_open:!!root?.classList.contains('show'),header_has_xmlid:header.getAttribute('data-menu-xmlid')!==null,child_xmlids:children};
    });`));
}
async function collectCronCode(page) {
  return page.wait(inspect(`const form=[...document.querySelectorAll('.o_form_view')].find(el=>visible(el)&&field(el,'code'));
    const widget=field(form,'code'); const lines=[...(widget?.querySelectorAll('.ace_line')||[])].filter(visible);
    const value=lines.map(el=>(el.textContent||'').trim()).join(String.fromCharCode(10));
    if(!visible(widget)||!lines.length||value!=='model._process_pending()')return false;
    return {visible:true,value,origin:'visible_ace_lines'};`));
}
async function menuPage(page, role, key, options, inputs, state) {
  const menu = inputs.roleReport.roles[role].menus[key], xmlid = menuXmlid(key);
  await page.navigate(`${options.baseUrl}/odoo/action-${menu.action_id}?debug=1`);
  await page.wait(inspect(`return visible(document.querySelector('.o_list_view')) || visible(document.querySelector('.o_view_nocontent .o_nocontent_help'));`));
  if (key !== 'sales') {
    await page.wait(inspect(`const el=[...document.querySelectorAll('[data-menu-xmlid="portfolio_order_bridge.menu_import_root"]')].find(visible);
      if(!el)return false; el.click(); return true;`));
  }
  await page.wait(inspect(`return [...document.querySelectorAll('[data-menu-xmlid="${xmlid}"]')].some(visible);`));
  const seen = await collectVisibleMenus(page);
  for (const item of seen) state.observedMenus.add(item);
  for (const header of await collectImportHeaders(page)) {
    if (!state.config_headers.some(previous => equal(previous, header))) state.config_headers.push(header);
  }
  await page.evaluate(inspect(`const el=[...document.querySelectorAll('[data-menu-xmlid="${xmlid}"]')].find(visible); if(!el)return false; el.click(); return true;`));
  await page.wait(inspect(`return visible(document.querySelector('.o_list_view')) || visible(document.querySelector('.o_view_nocontent .o_nocontent_help'));`));
}
async function metadata(page, expectedId) {
  await page.wait(inspect(`const button=[...document.querySelectorAll('.o_debug_manager button')].find(visible); if(!button)return false;button.click();return true;`));
  await page.wait(inspect(`const item=[...document.querySelectorAll('.dropdown-item')].find(el=>visible(el)&&text(el)==='Metadata'); if(!item)return false;item.click();return true;`));
  await page.wait(inspect(`const modal=[...document.querySelectorAll('.modal')].find(el=>visible(el)&&['Metadata','View Metadata'].includes(text(el.querySelector('.modal-title'))));
    return modal&&[...modal.querySelectorAll('tr')].some(row=>text(row.querySelector('th'))==='Creation User:'&&text(row.querySelector('td')));`));
  const actual = await page.evaluate(inspect(`const modal=[...document.querySelectorAll('.modal')].find(el=>visible(el)&&['Metadata','View Metadata'].includes(text(el.querySelector('.modal-title'))));
    const rows=[...modal.querySelectorAll('tr')]; const get=label=>rows.find(row=>text(row.querySelector('th'))===label)?.querySelector('td');
    return {visible:visible(modal),creator:text(get('Creation User:')),record_id:text(get('ID:'))};`));
  requireEvidence(numeric(actual.record_id) === expectedId);
  await page.evaluate(inspect(`const modal=[...document.querySelectorAll('.modal')].find(el=>visible(el)&&['Metadata','View Metadata'].includes(text(el.querySelector('.modal-title'))));
    const close=modal.querySelector('button.btn-close,button.o_dialog_close'); if(!close)return false;close.click();return true;`));
  await page.wait(inspect(`return ![...document.querySelectorAll('.modal')].some(el=>visible(el)&&['Metadata','View Metadata'].includes(text(el.querySelector('.modal-title'))));`));
  return {visible: actual.visible, creator: actual.creator};
}
async function capture(page, role, key, state, options, inputs) {
  requireEvidence(PAGES[role].includes(key));
  requireEvidence(await page.evaluate(inspect(`return visible(document.querySelector('.o_main_navbar')) && !document.querySelector('input[name=password],input[type=password]') && !document.querySelector('.o_error_dialog');`)));
  const shot = await captureSettledScreenshot(page);
  requireEvidence(typeof shot.data === 'string' && shot.data.length > 0);
  writeOutput(path.join(options.outputDir, screenshotName(role, key)), Buffer.from(shot.data, 'base64'), inputs);
  state.pages[key].screenshot = screenshotName(role, key);
}
async function sourcePages(page, role, options, inputs, state, progress) {
  progress.stage = 'source'; await menuPage(page, role, 'sources', options, inputs, state);
  await clickVisibleRecord(page, 'name', LABEL.source);
  await page.wait(inspect(`const form=document.querySelector('.o_form_view');return visible(form)&&read(form,'name').value===${JSON.stringify(LABEL.source)};`));
  state.pages.source = await page.evaluate(inspect(`const form=document.querySelector('.o_form_view');return {visible:visible(form),model:${JSON.stringify(SOURCE)},
    fields:Object.fromEntries(${JSON.stringify(['name', 'company_id', 'company_code', 'code', 'sales_user_id', 'processor_user_id', 'integration_user_ids', 'processing_cron_id'])}.map(name=>{const value=read(form,name);if(name==='integration_user_ids') value.value=[...(field(form,name)?.querySelectorAll('.o_tag_badge_text')||[])].filter(visible).map(text);return [name,value];}))};`));
  await capture(page, role, 'source', state, options, inputs);
  progress.stage = 'cron';
  await page.wait(inspect(`const el=field(document.querySelector('.o_form_view'),'processing_cron_id');
    const link=[...(el?.querySelectorAll('a,button.o_external_button')||[])].find(visible);if(!link)return false;link.click();return true;`));
  await page.wait(inspect(`const form=[...document.querySelectorAll('.o_form_view')].find(el=>visible(el)&&read(el,'name').value===${JSON.stringify('Order import / 订单导入: ' + LABEL.source)});return form&&read(form,'active').visible;`));
  state.pages.cron = await page.evaluate(inspect(`const form=[...document.querySelectorAll('.o_form_view')].find(el=>visible(el)&&read(el,'name').value===${JSON.stringify('Order import / 订单导入: ' + LABEL.source)});
    return {visible:visible(form),model:'ir.cron',opened_by_link:true,fields:Object.fromEntries(['name','active','user_id'].map(name=>[name,read(form,name)]))};`));
  state.pages.cron.fields.code = await collectCronCode(page);
  await capture(page, role, 'cron', state, options, inputs);
  // Navigation discards no changes: the configured cron and its toggle were only read.
  for (const key of ['customer_maps', 'product_maps']) {
    progress.stage = key; await menuPage(page, role, key, options, inputs, state);
    const reference = key === 'customer_maps' ? 'external_ref' : 'external_sku';
    await page.wait(inspect(`const root=document.querySelector('.o_list_view');return visible(root)&&!root.closest('.o_view_sample_data')&&[...root.querySelectorAll('tr.o_data_row td[name="${reference}"]')].some(el=>visible(el)&&text(el).startsWith('BRIDGE-ACCEPTANCE-'));`));
    state.pages[key] = await page.evaluate(inspect(`const root=document.querySelector('.o_list_view');const columns=[...root.querySelectorAll('th[data-name]')].filter(visible).map(el=>el.dataset.name);
      const names=${JSON.stringify(['source_id', 'company_id', reference, key === 'customer_maps' ? 'partner_id' : 'product_id', 'active'])};
      const rows=[...root.querySelectorAll('tr.o_data_row')].filter(visible).map(row=>({visible:visible(row),...Object.fromEntries(names.map(name=>{
        const cell=row.querySelector('td[name="'+name+'"]'); const checkbox=cell?.querySelector('input[type=checkbox]'); return [name,name==='active'?!!checkbox?.checked:text(cell)];}))}));
      return {visible:visible(root),model:${JSON.stringify(MODELS[key])},columns,rows};`));
    await capture(page, role, key, state, options, inputs);
  }
}
function eventKey(job, kind, ordinal) {
  const matches = EVENTS.filter(([, suffix, event, no]) => external(suffix) === job && event === kind && no === numeric(ordinal));
  requireEvidence(matches.length === 1); return matches[0][0];
}
async function historyRows(page, nested, suffix) {
  const rows = await page.evaluate(inspect(`const root=${nested ? "field(document.querySelector('.o_form_view'),'attempt_ids')" : "document.querySelector('.o_list_view')"};
    return [...(root?.querySelectorAll('tr.o_data_row')||[])].filter(visible).map(row=>{const cell=name=>row.querySelector('td[name="'+name+'"]');
      const names=['attempt_no','event_type','actor_id','retry_reason'${nested ? '' : ",'job_id'"}];
      return {visible:visible(row)&&names.every(name=>visible(cell(name))),job:${nested ? JSON.stringify(external(suffix)) : "text(cell('job_id'))"},
        attempt_no:text(cell('attempt_no')),event_type:eventType(text(cell('event_type'))),actor:text(cell('actor_id')),retry_reason:text(cell('retry_reason'))};});`));
  return rows.map(row => ({...row, event_key: eventKey(row.job, row.event_type, row.attempt_no)}));
}
async function inspectAndRedactAudit(page) {
  // Only presence and readonly facts leave the page; raw text stays in DOM memory.
  const evidence = await page.evaluate(inspect(`const form=document.querySelector('.o_form_view');
    return {visible:visible(form),model:${JSON.stringify(JOB)},readonly:!!form.querySelector('.o_form_readonly')||form.classList.contains('o_form_readonly'),
      fields:Object.fromEntries(['raw_payload','canonical_payload','payload_hash'].map(name=>{const original=read(form,name);return [name,{visible:original.visible,readonly:original.readonly,value:null,nonempty:original.value.length>0}];}))};`));
  requireEvidence(evidence.visible && evidence.readonly);
  for (const field of Object.values(evidence.fields)) requireEvidence(field.visible && field.readonly && field.nonempty);
  requireEvidence(await page.evaluate(inspect(`const form=document.querySelector('.o_form_view'); for(const name of ['raw_payload','canonical_payload','payload_hash']) {
    const el=field(form,name);if(!visible(el))return false;el.replaceChildren(document.createTextNode('[Audit content redacted]')); } return true;`)));
  await page.wait(inspect(`const form=document.querySelector('.o_form_view'); return ['raw_payload','canonical_payload','payload_hash']
    .every(name=>visible(field(form,name))&&text(field(form,name))==='[Audit content redacted]');`));
  return evidence;
}
async function auditListReadonly(page) {
  return page.evaluate(inspect(`const rows=[...document.querySelectorAll('.o_list_view .o_list_renderer tr.o_data_row')].filter(visible);
    return rows.length>0 && rows.every(row=>![...row.querySelectorAll('input:not([type=checkbox]),textarea,[contenteditable=true]')]
      .some(el=>visible(el)&&!el.disabled&&!el.readOnly));`));
}
async function jobPages(page, role, options, inputs, state, progress) {
  for (const suffix of ['success', 'repaired']) {
    const key = `job_${suffix}`; progress.stage = key;
    await menuPage(page, role, 'jobs', options, inputs, state);
    const listButtons = await page.evaluate(inspect(`return [...new Set([...document.querySelectorAll('.o_list_view button[name],.o_control_panel button[name]')].filter(visible).map(el=>el.name))].sort();`));
    requireEvidence(equal(listButtons, role === 'operator' ? ['action_process_pending'] : []));
    state.job_list_buttons = listButtons;
    await clickVisibleRecord(page, 'external_order_id', external(suffix));
    await page.wait(inspect(`const form=document.querySelector('.o_form_view');return visible(form)&&read(form,'external_order_id').value===${JSON.stringify(external(suffix))};`));
    state.pages[key] = await page.evaluate(inspect(`const form=document.querySelector('.o_form_view');const fields=Object.fromEntries(${JSON.stringify(['external_order_id', 'source_id', 'company_id', 'state', 'attempt_count', 'submitted_by_id', 'sale_order_id'])}.map(name=>[name,read(form,name)]));
      fields.state.value=status(fields.state.value);
      return {visible:visible(form),model:${JSON.stringify(JOB)},readonly:!!form.querySelector('.o_form_readonly')||form.classList.contains('o_form_readonly'),
        opened_from_visible_row:true,fields,write_controls:controls(),visible_buttons:[...form.querySelectorAll('button[name]')].filter(el=>visible(el)&&['action_open_retry','action_process_pending','action_open_sale_order'].includes(el.name)).map(el=>el.name)};`));
    await tab(page, ['History / 处理记录']);
    await page.wait(inspect(`const root=field(document.querySelector('.o_form_view'),'attempt_ids');return [...(root?.querySelectorAll('tr.o_data_row')||[])].filter(visible).length===${suffix === 'success' ? 1 : 3};`));
    state.pages[key].history = await historyRows(page, true, suffix);
    state.pages[key].metadata = await metadata(page, inputs.flow.jobs[suffix].id);
    await capture(page, role, key, state, options, inputs);
    const auditKey = `audit_${suffix}`; progress.stage = auditKey;
    await tab(page, ['Reception audit / 接收审计']);
    await page.wait(inspect(`const form=document.querySelector('.o_form_view');return ['raw_payload','canonical_payload','payload_hash'].every(name=>read(form,name).visible&&text(field(form,name)).length>0);`));
    state.pages[auditKey] = await inspectAndRedactAudit(page);
    await capture(page, role, auditKey, state, options, inputs);
  }
  progress.stage = 'attempts'; await menuPage(page, role, 'attempts', options, inputs, state);
  await page.wait(inspect(`return [...document.querySelectorAll('.o_list_view tr.o_data_row')].filter(visible).length===4;`));
  const view = await page.evaluate(inspect(`const root=document.querySelector('.o_list_view');return {visible:visible(root),model:${JSON.stringify(EVENT)},readonly:true,write_controls:controls()};`));
  // Readonly here is native absence of create/edit/delete and absence of editable cells.
  requireEvidence(await auditListReadonly(page));
  state.pages.attempts = {...view, rows: await historyRows(page, false)};
  await capture(page, role, 'attempts', state, options, inputs);
}
async function quotePages(page, role, options, inputs, state, progress) {
  for (const suffix of ['success', 'repaired']) {
    const key = `quote_${suffix}`; progress.stage = key;
    await menuPage(page, role, 'sales', options, inputs, state);
    await clickVisibleRecord(page, 'name', inputs.flow.quotes[suffix].name);
    await page.wait(inspect(`const form=document.querySelector('.o_form_view');return visible(form)&&read(form,'name').value===${JSON.stringify(inputs.flow.quotes[suffix].name)};`));
    const data = await page.evaluate(inspect(`const form=document.querySelector('.o_form_view'),totals=field(form,'tax_totals');
      const fields=Object.fromEntries(['name','partner_id','state'].map(name=>[name,read(form,name)]));fields.state.value=status(fields.state.value);
      fields.tax_totals={visible:visible(totals)&&visible(totals?.querySelector('span[name=amount_total]')),
        value:text(totals?.querySelector('span[name=amount_total]')),readonly:!!totals?.classList.contains('o_readonly_modifier')&&!totals?.querySelector('input,.o_tax_group_edit'),
        widget:totals?.classList.contains('o_field_account-tax-totals-field')?'account-tax-totals-field':''};
      const rows=[...(field(form,'order_line')?.querySelectorAll('tr.o_data_row')||[])].filter(visible).map(row=>{const cell=name=>row.querySelector('td[name="'+name+'"]');return {
        visible:visible(row),product:text(visible(cell('product_template_id'))?cell('product_template_id'):cell('product_id')),product_visible:visible(cell('product_template_id'))||visible(cell('product_id')),unit:text(cell('product_uom')),unit_visible:visible(cell('product_uom')),
        quantity:text(cell('product_uom_qty')),price_unit:text(cell('price_unit')),discount:visible(cell('discount'))?text(cell('discount')):'',tax:text(cell('tax_id')),subtotal:text(cell('price_subtotal')),
        unit_evidence:{visible:visible(cell('product_uom')),origin:visible(cell('product_uom'))?'native_column':'native_feature_disabled',native_column_absent:!cell('product_uom')},
        discount_evidence:{visible:visible(cell('discount')),origin:visible(cell('discount'))?'native_column':'visible_quantity_price_subtotal',native_column_absent:!cell('discount')},
        visibility:{quantity:visible(cell('product_uom_qty')),price_unit:visible(cell('price_unit')),subtotal:visible(cell('price_subtotal')),discount:visible(cell('discount')),tax:visible(cell('tax_id'))}};});
      const submit=[...form.querySelectorAll('button[name=action_portfolio_submit]')].find(visible);
      return {visible:visible(form),model:'sale.order',opened_from_visible_row:true,fields,lines:rows,company_scope:companyScope(),
        approval_required:{visible:visible(submit),value:visible(submit),submit_button_visible:visible(submit),approval_tab_visible:false}};`));
    for (const line of data.lines) if (line.visibility.discount === false) {
      line.discount_evidence.derived_percent = Math.round((1 - numeric(line.subtotal) / (numeric(line.quantity) * numeric(line.price_unit))) * 100000000) / 1000000;
    }
    await tab(page, ['Other Info', 'Other Information']);
    const other = await page.evaluate(inspect(`const form=document.querySelector('.o_form_view');return Object.fromEntries(['user_id','origin','client_order_ref'].map(name=>[name,read(form,name)]));`));
    Object.assign(data.fields, other);
    await tab(page, ['Sales Approval']);
    data.fields.portfolio_approval_state = await page.evaluate(inspect(`const value=read(document.querySelector('.o_form_view'),'portfolio_approval_state');value.value=status(value.value);return value;`));
    data.approval_required.approval_tab_visible = await page.evaluate(inspect(`return [...document.querySelectorAll('.o_notebook .nav-link')].some(el=>visible(el)&&el.classList.contains('active')&&text(el)==='Sales Approval');`));
    data.metadata = await metadata(page, inputs.flow.quotes[suffix].id);
    state.pages[key] = data;
    // Show quotation lines and totals again; each tab's visible fields were collected while active.
    await tab(page, ['Order Lines']);
    await capture(page, role, key, state, options, inputs);
  }
}
async function otherSalesPage(page, role, options, inputs, state, progress) {
  progress.stage = 'sales'; await menuPage(page, role, 'sales', options, inputs, state);
  await page.wait(inspect(`return visible(document.querySelector('.o_view_nocontent .o_nocontent_help'));`));
  const list = await page.evaluate(inspect(`const root=document.querySelector('.o_list_view'),empty=document.querySelector('.o_view_nocontent .o_nocontent_help');
    const cells=[...(root?.querySelectorAll('tr.o_data_row td[name=name]')||[])].filter(visible);
    return {visible:visible(root)||visible(empty),model:'sale.order',unfiltered_names:cells.map(text),
      has_native_sample_data:!!root?.closest('.o_view_sample_data')||!!root?.querySelector('.o_view_sample_data'),
      empty_state:{visible:visible(empty),selector:'.o_view_nocontent .o_nocontent_help',text:text(empty)}};`));
  const classified = classifyBusinessRows(list.unfiltered_names, list.has_native_sample_data, list.empty_state, role, inputs);
  state.pages.sales = {...list, ...classified, foreign_quotes_absent: !list.unfiltered_names.some(name => Object.values(inputs.flow.quotes).some(quote => quote.name === name))};
  await capture(page, role, 'sales', state, options, inputs);
}
async function withRolePage(cdp, inspectPage) {
  const context = await cdp.call('Target.createBrowserContext', {disposeOnDetach: true});
  try {
    const target = await cdp.call('Target.createTarget', {url: 'about:blank', browserContextId: context.browserContextId});
    const attached = await cdp.call('Target.attachToTarget', {targetId: target.targetId, flatten: true});
    const page = new Page(cdp, attached.sessionId);
    await page.call('Page.enable'); await page.call('Runtime.enable');
    await page.call('Emulation.setDeviceMetricsOverride', {width: 1600, height: 1150, deviceScaleFactor: 1, mobile: false});
    return await inspectPage(page);
  } finally { await cdp.call('Target.disposeBrowserContext', {browserContextId: context.browserContextId}).catch(() => {}); }
}
async function checkRole(cdp, role, options, inputs, progress) {
  return withRolePage(cdp, async page => {
    progress.stage = 'login'; const state = {role, login: await login(page, role, options, inputs), observedMenus: new Set(), config_headers: [], pages: {}};
    requireEvidence(state.login.password_field_count === 0);
    if (role === 'settings') await sourcePages(page, role, options, inputs, state, progress);
    if (['operator', 'integration'].includes(role)) await jobPages(page, role, options, inputs, state, progress);
    if (['operator', 'sales'].includes(role)) await quotePages(page, role, options, inputs, state, progress);
    if (role === 'other_sales') await otherSalesPage(page, role, options, inputs, state, progress);
    state.menu_xmlids = [...state.observedMenus]; delete state.observedMenus;
    state.import_root_visible = await page.evaluate(inspect(`return [...document.querySelectorAll('[data-menu-xmlid="portfolio_order_bridge.menu_import_root"]')].some(visible);`));
    state.import_payload_entry_count = await page.evaluate(inspect(`return [...document.querySelectorAll('[name=raw_payload] textarea,[name=canonical_payload] textarea,[name=payload] textarea,[name=order_payload] textarea,input[type=file]')].filter(el=>visible(el)&&!el.readOnly&&!el.disabled).length;`));
    validateDomEvidence(role, state, inputs); return state;
  });
}
async function main(argv = process.argv.slice(2)) {
  const progress = {role: 'input', stage: 'validation'};
  let options, inputs, profile, chrome, cdp, launch, ownedProfile, startup;
  try {
    options = parseArgs(argv); inputs = readInputs(options);
    progress.stage = 'chrome_resolution'; const binary = resolveChrome(options.chromePath); requireEvidence(typeof global.WebSocket === 'function');
    fs.mkdirSync(options.outputDir, {recursive: true}); runtimePath(options.outputDir, {existing: true, directory: true});
    profile = fs.mkdtempSync(path.join(options.outputDir, '.chrome-')); fs.chmodSync(profile, 0o700); ownedProfile = fs.lstatSync(profile);
    progress.stage = 'chrome_startup';
    launch = createChromeLaunch(['--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
      '--disable-component-update', '--disable-sync', '--disable-extensions', '--disable-default-apps', '--metrics-recording-only',
      '--no-pings', '--no-sandbox', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
      `--user-data-dir=${profile}`, `--disk-cache-dir=${path.join(profile, 'cache')}`, 'about:blank']);
    startup = {binary, started: Date.now(), stderr: Buffer.alloc(0)};
    chrome = childProcess.spawn(binary, launch.args, {env: launch.env, stdio: ['ignore', 'ignore', 'pipe']});
    chrome.stderr.on('data', chunk => { const remaining = 8192 - startup.stderr.length; if (remaining > 0) startup.stderr = Buffer.concat([startup.stderr, chunk.subarray(0, remaining)]); });
    let failed = false; chrome.on('error', error => { failed = true; startup.spawn_code = error.code; });
    const portFile = path.join(profile, 'DevToolsActivePort'), deadline = Date.now() + 15000;
    startup.portFile = portFile;
    while (!fs.existsSync(portFile) && !failed && chrome.exitCode === null && Date.now() < deadline) await delay(100);
    requireEvidence(!failed && fs.existsSync(portFile));
    const [port, route] = fs.readFileSync(portFile, 'utf8').trim().split('\n');
    requireEvidence(/^\d+$/.test(port) && Number(port) > 0 && Number(port) < 65536 && /^\/devtools\/browser\/[\w-]+$/.test(route));
    cdp = await Cdp.connect(`ws://127.0.0.1:${port}${route}`);
    const evidence = {};
    for (const role of ROLES) { progress.role = role; evidence[role] = await checkRole(cdp, role, options, inputs, progress); }
    const report = validateBrowserReport(evidence, inputs); progress.stage = 'output';
    writeOutput(path.join(options.outputDir, 'report.json'), JSON.stringify(report, null, 2) + '\n', inputs);
    process.stdout.write(JSON.stringify(report, null, 2) + '\n'); return 0;
  } catch {
    const report = safeFailure(progress);
    if (startup && progress.stage === 'chrome_startup') report.startup_diagnostics = chromeStartupDiagnostics({stderr: startup.stderr.toString('utf8'),
      binary: startup.binary, spawn_code: startup.spawn_code, exit_code: chrome?.exitCode, signal: chrome?.signalCode, duration_ms: Date.now() - startup.started,
      port_file_present: fs.existsSync(startup.portFile), tmpdir_exists: !!launch && fs.existsSync(launch.ipcDirectory),
      timed_out: Date.now() - startup.started >= 15000, debugging_loopback: true});
    if (profile && inputs) { try { writeOutput(path.join(options.outputDir, 'report.json'), JSON.stringify(report, null, 2) + '\n', inputs); } catch {} }
    process.stdout.write(JSON.stringify(report) + '\n'); return 1;
  } finally {
    if (cdp) cdp.close();
    if (chrome && chrome.exitCode === null && chrome.signalCode === null) {
      chrome.kill('SIGTERM'); const deadline = Date.now() + 5000;
      while (chrome.exitCode === null && chrome.signalCode === null && Date.now() < deadline) await delay(100);
      if (chrome.exitCode === null && chrome.signalCode === null) {
        chrome.kill('SIGKILL'); await new Promise(resolve => { chrome.once('exit', resolve); setTimeout(resolve, 3000); });
      }
    }
    // Both short IPC and profile cleanup require the process dead and the exact owned inode.
    if (launch) launch.cleanup(chrome);
    if (profile) {
      const current = fs.lstatSync(profile);
      requireEvidence((!chrome || chrome.exitCode !== null || chrome.signalCode !== null) && current.isDirectory() && !current.isSymbolicLink() &&
        current.uid === process.getuid() && current.dev === ownedProfile.dev && current.ino === ownedProfile.ino);
      fs.rmSync(profile, {recursive: true, force: false});
    }
  }
}
module.exports = {parseArgs, readInputs, validateSourceReports, validateRoleReport, validateDomEvidence, validateBrowserReport,
  classifyBusinessRows, clickVisibleRecord, collectVisibleMenus, collectImportHeaders, collectCronCode, auditListReadonly, waitForRenderedPane, captureSettledScreenshot, withRolePage, inspectAndRedactAudit, safeFailure, main};
if (require.main === module) main().then(status => { process.exitCode = status; }).catch(() => {
  process.stdout.write(JSON.stringify(safeFailure()) + '\n'); process.exitCode = 1;
});
