'use strict';

// Pure Node built-in tests. No Chrome, HTTP service, credentials or Odoo import.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const ROOT = path.resolve(__dirname, '..');
const SCRIPT = path.join(ROOT, 'scripts', 'browser_m2c_check.js');
const ROLES = ['sales', 'manager', 'manager2', 'buyer', 'warehouse', 'finance', 'other_sales'];
const FINANCIAL = ['draft_invoice_qty', 'posted_invoice_qty', 'draft_credit_qty',
  'posted_credit_qty', 'posted_signed_amount', 'invoice_ids', 'credit_note_ids'];
const MODELS = {sale_order: 'sale.order', purchase_order: 'purchase.order',
  receipt_first: 'stock.picking', receipt_second: 'stock.picking',
  delivery_first: 'stock.picking', delivery_second: 'stock.picking',
  return_picking: 'stock.picking', invoice_first: 'account.move',
  invoice_second: 'account.move', credit_note: 'account.move'};

function api() {
  assert.ok(fs.existsSync(SCRIPT), 'Seven-role rendered-browser verifier is missing');
  const result = require(SCRIPT);
  for (const name of ['parseArgs', 'readInputs', 'validateRoleReport', 'validateDomEvidence',
    'validateBrowserReport', 'main']) {
    assert.equal(typeof result[name], 'function', `Browser API ${name} is missing`);
  }
  return result;
}

function fixture() {
  const identityModels = {company_a: 'res.company', company_b: 'res.company',
    warehouse_a: 'stock.warehouse', customer: 'res.partner', vendor: 'res.partner',
    initial_bearing_move: 'stock.move',
    ...Object.fromEntries(ROLES.map(role => [`user_${role}`, 'res.users'])),
    ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(key => [`product_${key}`, 'product.product'])),
    ...Object.fromEntries(['bearing', 'sensor', 'bolt'].map(key => [`supplier_${key}`, 'product.supplierinfo']))};
  const acceptance = {namespace: 'portfolio_trade_acceptance', identities:
    Object.fromEntries(Object.entries(identityModels).map(([key, model], index) =>
      [key, {model, id: index + 101}]))};
  const flow = {database: 'odoo_portfolio', namespace: 'portfolio_trade_fulfillment',
    company_id: 101, identities: Object.fromEntries(Object.entries(MODELS).map(([key, model], index) =>
      [key, {model, id: index + 701, name: `M2C-${key}`} ])),
    actors: Object.fromEntries(['sales', 'manager', 'buyer', 'warehouse', 'finance'].map(role =>
      [role, acceptance.identities[`user_${role}`].id])),
    physical: {delivered: 100, returned: 10, net: 90, stock: 10},
    financial: {invoice_posted: 100, credit_posted: 10, signed_posted: 90,
      net_amount: 7650, currency: 'CNY'}};
  const roles = {};
  for (const role of ROLES) {
    const company = role === 'other_sales' ? 102 : 101;
    const keys = role === 'buyer' ? ['purchases'] : role === 'warehouse' ? ['stock'] :
      role === 'finance' ? ['sales', 'invoices'] : ['sales'];
    const models = {sales: 'sale.order', purchases: 'purchase.order', stock: 'stock.picking', invoices: 'account.move'};
    const names = {sales: '销售订单', purchases: '采购订单', stock: '收发货', invoices: '客户发票'};
    roles[role] = {uid: acceptance.identities[`user_${role}`].id, http_status: 200,
      company_id: company, allowed_company_ids: [company], active_company_ids: [company],
      menus: Object.fromEntries(keys.map((key, index) => [key, {id: 201 + index, name: names[key],
        xmlid: `portfolio_trade.menu_portfolio_${key}`, action_id: 301 + index, model: models[key]}])),
      actions: Object.fromEntries(keys.map((key, index) => [key, {id: 301 + index,
        res_model: models[key], view_mode: 'list,form'}])),
      source_route: `/odoo/action-301/${flow.identities.sale_order.id}`,
      documents: Object.fromEntries(Object.entries(flow.identities)
        .filter(([, value]) => role === 'other_sales' ? false : role === 'warehouse' ?
          value.model === 'stock.picking' : role === 'finance' ?
            ['sale.order', 'account.move'].includes(value.model) : value.model !== 'account.move')
        .map(([key, value]) => [key, value.id])),
      checks: {authenticated: true, company_scope: true, native_records_read: true},
      facts: role === 'finance' ? {gross_delivered_qty: 100, returned_qty: 10,
        net_delivered_qty: 90, posted_invoice_qty: 100, posted_credit_qty: 10, posted_signed_amount: 7650} : {}};
  }
  return {database: 'odoo_portfolio', acceptance, flow,
    roleReport: {schema_version: 1, ok: true, database: 'odoo_portfolio', roles}};
}

function dom(role, inputs = fixture()) {
  const report = inputs.roleReport.roles[role];
  const sourceName = inputs.flow.identities.sale_order.name;
  const traceAllowed = !['warehouse', 'other_sales'].includes(role);
  const pageModel = role === 'warehouse' ? 'stock.picking' : role === 'buyer' ? 'purchase.order' : 'sale.order';
  return {role,
    login: {rendered_form: true, submitted: true, authenticated: true,
      user_label: `Acceptance ${role}`, password_field_count: 0},
    page: {visible: true, model: pageModel,
      menu_labels: Object.values(report.menus).map(menu => menu.name),
      record_names: role === 'other_sales' ? [] : Object.keys(report.documents)
        .filter(key => inputs.flow.identities[key].model === pageModel)
        .map(key => inputs.flow.identities[key].name), foreign_source_absent: role === 'other_sales',
      record_name_visibility: Object.keys(report.documents).filter(key =>
        inputs.flow.identities[key].model === pageModel).map(() => true),
      empty_state: role === 'other_sales' ? {visible: true,
        selector: '.o_view_nocontent .o_nocontent_help', text: 'No data to display'} : null},
    source: traceAllowed ? {visible: true, name: sourceName,
      readonly: ['buyer', 'finance'].includes(role), write_controls: []} : null,
    trace: traceAllowed ? {opened_by_click: true, visible: true, source_name: sourceName,
      quantities: {gross_delivered_qty: '100.00', returned_qty: '10.00', net_delivered_qty: '90.00'},
      quantity_visibility: {gross_delivered_qty: true, returned_qty: true, net_delivered_qty: true},
      line_rows: [{product: 'Acceptance bearing6205', unit: 'Units', visible: true,
        product_visible: true, unit_visible: true,
        quantity_visibility: {gross_delivered_qty: true, returned_qty: true, net_delivered_qty: true},
        gross_delivered_qty: '100.00', returned_qty: '10.00', net_delivered_qty: '90.00'}],
      linked_document_names: Object.keys(report.documents).filter(key => key !== 'sale_order')
        .map(key => inputs.flow.identities[key].name),
      financial_field_names: role === 'finance' ? FINANCIAL : [],
      financial_values: role === 'finance' ? {posted_invoice_qty: '100.00', posted_credit_qty: '10.00',
        posted_signed_amount: '¥ 7,650.00'} : {}} : null,
    invoice: role === 'finance' ? {visible: true, model: 'account.move',
      name: inputs.flow.identities.invoice_first.name} : null,
    screenshot: `${role}.png`};
}

function stockList(inputs = fixture()) {
  const names = Object.entries(inputs.flow.identities).filter(([, item]) => item.model === 'stock.picking')
    .map(([, item]) => item.name);
  return {visible: true, model: 'stock.picking', columns: ['name', 'origin', 'state'],
    column_visibility: {name: true, origin: true, state: true},
    record_names: names, record_name_visibility: names.map(() => true)};
}

test('module import is guarded and does not launch a browser or read credentials', () => {
  assert.ok(fs.existsSync(SCRIPT), 'Seven-role rendered-browser verifier is missing');
  const childProcess = require('node:child_process');
  const originalSpawn = childProcess.spawn;
  const originalRead = fs.readFileSync;
  const originalWebSocket = global.WebSocket;
  try {
    childProcess.spawn = () => { throw new Error('Browser spawned during import'); };
    fs.readFileSync = function(file, ...rest) {
      assert.ok(!String(file).includes('acceptance-accounts'), 'Credentials read during import');
      return originalRead.call(this, file, ...rest);
    };
    global.WebSocket = class { constructor() { throw new Error('CDP connected during import'); } };
    delete require.cache[SCRIPT];
    api();
  } finally {
    childProcess.spawn = originalSpawn;
    fs.readFileSync = originalRead;
    global.WebSocket = originalWebSocket;
  }
});

test('arguments accept the documented local contract and reject remote or unknown options', () => {
  const verifier = api();
  const argv = ['--base-url', 'http://127.0.0.1:8069', '--database', 'odoo_portfolio',
    '--credentials', '.runtime/accounts.json', '--acceptance-report', '.runtime/acceptance.json',
    '--flow-report', '.runtime/flow.json', '--role-report', '.runtime/roles.json'];
  const options = verifier.parseArgs(argv);
  assert.equal(options.database, 'odoo_portfolio');
  assert.equal(options.outputDir, path.join(ROOT, '.runtime', 'm2c-browser'));
  for (const [flag, value] of [['--base-url', 'https://127.0.0.1:8069'],
    ['--base-url', 'http://localhost:8069'], ['--base-url', 'http://127.0.0.1:8069@remote.invalid'],
    ['--base-url', 'http://127.0.0.1:8069/path'], ['--database', 'production'],
    ['--output-dir', '../outside'], ['--password', 'PRIVATE-ARGUMENT']]) {
    const revised = argv.filter((_, index) => argv[index - 1] !== flag && argv[index] !== flag);
    assert.throws(() => verifier.parseArgs([...revised, flag, value]), {name: 'Error'});
  }
});

test('inputs require existing mode0600 nonsymlink credentials and never create a missing file', () => {
  const verifier = api();
  const base = path.join(ROOT, '.runtime', 'tests');
  fs.mkdirSync(base, {recursive: true});
  const directory = fs.mkdtempSync(path.join(base, 'browser-contract-'));
  try {
    const inputs = fixture();
    const files = {credentials: 'accounts', acceptanceReport: 'acceptance', flowReport: 'flow', roleReport: 'roles'};
    const documents = {credentials: {schema_version: 1, database: 'odoo_portfolio',
      accounts: Object.fromEntries(ROLES.map(role => [role, {login: `portfolio.acceptance.${role}`,
        password: `PRIVATE-${role}-${'x'.repeat(32)}`}]))},
    acceptanceReport: inputs.acceptance, flowReport: inputs.flow, roleReport: inputs.roleReport};
    const options = {baseUrl: 'http://127.0.0.1:8069', database: 'odoo_portfolio',
      outputDir: path.join(directory, 'screenshots')};
    for (const [key, filename] of Object.entries(files)) {
      options[key] = path.join(directory, `${filename}.json`);
      fs.writeFileSync(options[key], JSON.stringify(documents[key]), {mode: 0o600});
    }
    assert.equal(verifier.readInputs(options).database, 'odoo_portfolio');
    const original = fs.readFileSync(options.credentials);
    fs.unlinkSync(options.credentials);
    assert.throws(() => verifier.readInputs(options));
    assert.equal(fs.existsSync(options.credentials), false);
    fs.writeFileSync(options.credentials, original, {mode: 0o644});
    assert.throws(() => verifier.readInputs(options));
    const target = path.join(directory, 'private-target.json');
    fs.renameSync(options.credentials, target);
    fs.chmodSync(target, 0o600);
    fs.symlinkSync(target, options.credentials);
    assert.throws(() => verifier.readInputs(options));
    assert.deepEqual(fs.readFileSync(target), original);
    assert.equal(fs.existsSync(options.outputDir), false, 'Input validation must not launch or prepare Chrome');
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('HTTP role report must have all seven expected UIDs and exact allowed companies', () => {
  const verifier = api();
  const inputs = fixture();
  assert.doesNotThrow(() => verifier.validateRoleReport(inputs.roleReport, inputs));
  for (const mutate of [report => delete report.roles.manager2,
    report => report.roles.sales.uid = 999,
    report => report.roles.finance.allowed_company_ids.push(102),
    report => report.roles.other_sales.company_id = 101,
    report => report.database = 'odoo_portfolio_m1_test',
    report => report.roles.sales.source_route = 'https://remote.invalid/']) {
    const report = structuredClone(inputs.roleReport);
    mutate(report);
    assert.throws(() => verifier.validateRoleReport(report, inputs));
  }
});

test('login evidence requires rendered form submission and the actual role user label', () => {
  const verifier = api();
  for (const mutate of [e => e.login.rendered_form = false, e => e.login.submitted = false,
    e => e.login.authenticated = false, e => e.login.user_label = 'Administrator',
    e => e.login.password_field_count = 1]) {
    const evidence = dom('sales');
    mutate(evidence);
    assert.throws(() => verifier.validateDomEvidence('sales', evidence, fixture()));
  }
});

test('metadata, screenshot counts and menu labels alone cannot replace visible source and trace UI', () => {
  const verifier = api();
  for (const mutate of [e => e.page.visible = false, e => e.page.menu_labels = [],
    e => e.source.visible = false, e => e.trace.visible = false,
    e => e.trace.opened_by_click = false, e => e.trace.line_rows = [],
    e => e.trace.line_rows[0].unit = '', e => e.trace.linked_document_names = []]) {
    const evidence = dom('buyer');
    mutate(evidence);
    assert.throws(() => verifier.validateDomEvidence('buyer', evidence, fixture()));
  }
});

test('trace UI must show per-line units and the actual 100 minus 10 equals 90 quantities', () => {
  const verifier = api();
  assert.doesNotThrow(() => verifier.validateDomEvidence('sales', dom('sales'), fixture()));
  for (const mutate of [e => e.trace.quantities.net_delivered_qty = '100.00',
    e => e.trace.line_rows[0].returned_qty = '0.00',
    e => e.trace.source_name = 'Foreign SO',
    e => e.trace.linked_document_names.push('Foreign PO')]) {
    const evidence = dom('sales');
    mutate(evidence);
    assert.throws(() => verifier.validateDomEvidence('sales', evidence, fixture()));
  }
});

test('correct trace text cannot satisfy acceptance when units rows or physical quantities are hidden', () => {
  const verifier = api();
  const inputs = fixture();
  for (const role of ['sales', 'buyer', 'finance']) {
    assert.doesNotThrow(() => verifier.validateDomEvidence(role, dom(role), inputs));
    for (const mutate of [e => e.trace.line_rows[0].unit_visible = false,
      e => e.trace.line_rows[0].visible = false, e => e.trace.line_rows[0].product_visible = false,
      e => e.trace.quantity_visibility.returned_qty = false,
      e => e.trace.quantity_visibility.net_delivered_qty = false,
      e => e.trace.line_rows[0].quantity_visibility.gross_delivered_qty = false,
      e => delete e.trace.line_rows[0].unit_visible]) {
      const hidden = dom(role);
      mutate(hidden);
      assert.throws(() => verifier.validateDomEvidence(role, hidden, inputs));
    }
  }
});

test('nonbilling financial fields must be absent from the DOM including hidden elements', () => {
  const verifier = api();
  for (const role of ['sales', 'manager', 'manager2', 'buyer']) {
    const evidence = dom(role);
    evidence.trace.financial_field_names = ['posted_signed_amount'];
    assert.throws(() => verifier.validateDomEvidence(role, evidence, fixture()));
  }
});

test('finance needs an invoice page, readonly Sales controls and rendered 7650 and 90 facts', () => {
  const verifier = api();
  assert.doesNotThrow(() => verifier.validateDomEvidence('finance', dom('finance'), fixture()));
  for (const mutate of [e => e.invoice = null, e => e.source.readonly = false,
    e => e.source.write_controls.push('action_confirm'),
    e => e.trace.financial_values.posted_signed_amount = '¥ 8,500.00',
    e => e.trace.financial_values.posted_credit_qty = '0.00',
    e => e.trace.financial_field_names = []]) {
    const evidence = dom('finance');
    mutate(evidence);
    assert.throws(() => verifier.validateDomEvidence('finance', evidence, fixture()));
  }
});

test('warehouse renders stock without trace and PARTS-B renders its own empty Sales page', () => {
  const verifier = api();
  assert.doesNotThrow(() => verifier.validateDomEvidence('warehouse', dom('warehouse'), fixture()));
  assert.doesNotThrow(() => verifier.validateDomEvidence('other_sales', dom('other_sales'), fixture()));
  const warehouse = dom('warehouse');
  warehouse.page.model = 'sale.order';
  assert.throws(() => verifier.validateDomEvidence('warehouse', warehouse, fixture()));
  const other = dom('other_sales');
  other.page.record_names = ['M2C-sale_order'];
  assert.throws(() => verifier.validateDomEvidence('other_sales', other, fixture()));
  other.page.record_names = [];
  other.page.foreign_source_absent = false;
  assert.throws(() => verifier.validateDomEvidence('other_sales', other, fixture()));
});

test('native Warehouse stock list uses visible name origin state columns and actual flow records', () => {
  const verifier = api();
  assert.equal(typeof verifier.validateListEvidence, 'function', 'Native visible-list evidence validator is missing');
  const inputs = fixture();
  const list = stockList(inputs);
  assert.equal(list.record_names.length, 5);
  assert.doesNotThrow(() => verifier.validateListEvidence('stock', list, inputs, 'warehouse'));
  for (const mutate of [e => e.model = 'sale.order', e => e.record_names = [],
    e => e.record_names.pop(), e => e.visible = false, e => e.columns = ['name']]) {
    const invalid = structuredClone(list);
    mutate(invalid);
    assert.throws(() => verifier.validateListEvidence('stock', invalid, inputs, 'warehouse'));
  }
});

test('correct stock column names and record text cannot satisfy acceptance when headers or name cells are hidden', () => {
  const verifier = api();
  const inputs = fixture();
  assert.doesNotThrow(() => verifier.validateListEvidence('stock', stockList(inputs), inputs, 'warehouse'));
  for (const mutate of [e => e.column_visibility.origin = false, e => e.column_visibility.state = false,
    e => e.column_visibility.name = false, e => e.record_name_visibility[0] = false,
    e => delete e.column_visibility, e => delete e.record_name_visibility]) {
    const hidden = stockList(inputs);
    mutate(hidden);
    assert.throws(() => verifier.validateListEvidence('stock', hidden, inputs, 'warehouse'));
  }
  const hiddenPage = dom('warehouse');
  hiddenPage.page.record_name_visibility[0] = false;
  assert.throws(() => verifier.validateDomEvidence('warehouse', hiddenPage, inputs));
});

test('bounded list collection retries transitions and returns only the same validated target snapshot', async () => {
  const verifier = api();
  assert.equal(typeof verifier.collectValidatedList, 'function', 'Bounded validated-list collection is missing');
  const inputs = fixture();
  const target = {...stockList(inputs), has_native_sample_data: false, empty_state: null};
  const old = {...structuredClone(target), model: 'sale.order', record_names: ['OLD SALES ORDER'],
    record_name_visibility: [true]};
  const transition = {...structuredClone(target), visible: false, columns: [], column_visibility: {},
    record_names: [], record_name_visibility: []};
  const wrongRecords = {...structuredClone(target), record_names: ['UNRELATED STOCK'], record_name_visibility: [true]};
  const snapshots = [old, transition, wrongRecords, target];
  const reads = [];
  const fakePage = {evaluate: async () => {
    const snapshot = snapshots.shift();
    assert.ok(snapshot, 'Collector re-read after finding and validating the target snapshot');
    reads.push(snapshot);
    return snapshot;
  }};
  const result = await verifier.collectValidatedList(fakePage, 'DOM evidence only', 'stock', inputs, 'warehouse',
    {timeoutMs: 100, intervalMs: 0});
  assert.equal(reads.length, 4);
  assert.deepEqual(result.record_names, target.record_names);
  assert.deepEqual(result.column_visibility, target.column_visibility);
  assert.equal(result.visible, true);
  const hidden = structuredClone(target);
  hidden.column_visibility.state = false;
  let attempts = 0;
  await assert.rejects(() => verifier.collectValidatedList({evaluate: async () => { attempts += 1; return hidden; }},
    'DOM evidence only', 'stock', inputs, 'warehouse', {timeoutMs: 10, intervalMs: 0}));
  assert.ok(attempts > 1, 'A hidden persistent target must be retried and ultimately fail');
});

test('PARTS-B empty Sales list requires a rendered native empty state when there is no header', () => {
  const verifier = api();
  const inputs = fixture();
  const list = {visible: true, model: 'sale.order', columns: [], record_names: [],
    empty_state: {visible: true, selector: '.o_view_nocontent .o_nocontent_help', text: 'No data to display'}};
  assert.doesNotThrow(() => verifier.validateListEvidence('sales', list, inputs, 'other_sales'));
  for (const mutate of [e => delete e.empty_state, e => e.empty_state.visible = false,
    e => e.empty_state.text = '', e => e.empty_state.selector = 'metadata-only',
    e => e.visible = false, e => e.model = 'stock.picking',
    e => e.record_names = ['M2C-sale_order']]) {
    const invalid = structuredClone(list);
    mutate(invalid);
    assert.throws(() => verifier.validateListEvidence('sales', invalid, inputs, 'other_sales'));
  }
  assert.throws(() => verifier.validateListEvidence('sales', list, inputs, 'sales'));
});

test('final PARTS-B browser evidence cannot claim empty UI from metadata alone', () => {
  const verifier = api();
  const inputs = fixture();
  assert.doesNotThrow(() => verifier.validateDomEvidence('other_sales', dom('other_sales'), inputs));
  for (const mutate of [e => delete e.page.empty_state,
    e => e.page.empty_state.visible = false, e => e.page.empty_state.text = '',
    e => e.page.record_names = ['M2C-sale_order']]) {
    const invalid = dom('other_sales');
    mutate(invalid);
    assert.throws(() => verifier.validateDomEvidence('other_sales', invalid, inputs));
  }
});

test('native empty PARTS-B sample rows are placeholders and never accepted as A business records', () => {
  const verifier = api();
  assert.equal(typeof verifier.classifyListRows, 'function', 'Native sample-row classifier is missing');
  const inputs = fixture();
  const empty = {visible: true, selector: '.o_view_nocontent .o_nocontent_help', text: 'No data to display'};
  const placeholders = Array.from({length: 10}, (_, index) => `Sample quotation ${index + 1}`);
  const rows = verifier.classifyListRows(placeholders, true, empty, 'other_sales', inputs);
  assert.deepEqual(rows, {record_names: [], has_native_sample_data: true, sample_row_count: 10});
  const list = {visible: true, model: 'sale.order', columns: ['name'], empty_state: empty, ...rows};
  assert.doesNotThrow(() => verifier.validateListEvidence('sales', list, inputs, 'other_sales'));
  const native = verifier.classifyListRows(['M2C-sale_order'], false, null, 'sales', inputs);
  assert.deepEqual(native.record_names, ['M2C-sale_order']);
  assert.equal(native.sample_row_count, 0);
  for (const invoke of [
    () => verifier.classifyListRows(placeholders, true, null, 'other_sales', inputs),
    () => verifier.classifyListRows([inputs.flow.identities.sale_order.name], true, empty, 'other_sales', inputs),
    () => verifier.classifyListRows([inputs.flow.identities.sale_order.name], true, empty, 'sales', inputs),
    () => verifier.validateListEvidence('sales', {...list, has_native_sample_data: false}, inputs, 'other_sales'),
    () => verifier.validateListEvidence('sales', {...list, empty_state: null}, inputs, 'other_sales'),
    () => verifier.validateListEvidence('sales', {...list, record_names: [inputs.flow.identities.sale_order.name]}, inputs, 'other_sales'),
  ]) assert.throws(invoke);
  const unmarked = verifier.classifyListRows(placeholders, false, empty, 'other_sales', inputs);
  assert.throws(() => verifier.validateListEvidence('sales', {...list, ...unmarked}, inputs, 'other_sales'));
});

test('sample-data evidence remains explicit in the safe B browser result and is rejected for A', () => {
  const verifier = api();
  const inputs = fixture();
  const other = dom('other_sales');
  Object.assign(other.page, {has_native_sample_data: true, sample_row_count: 10});
  const safe = verifier.validateDomEvidence('other_sales', other, inputs);
  assert.equal(safe.has_native_sample_data, true);
  assert.equal(safe.sample_row_count, 10);
  const sales = dom('sales');
  Object.assign(sales.page, {has_native_sample_data: true, sample_row_count: 10});
  assert.throws(() => verifier.validateDomEvidence('sales', sales, inputs));
});

test('failure DOM diagnostics whitelist structure and never include credential or page text', () => {
  const verifier = api();
  assert.equal(typeof verifier.sanitizeDiagnostics, 'function', 'Safe bounded DOM diagnostic sanitizer is missing');
  const safe = verifier.sanitizeDiagnostics({document_ready: true, navbar_present: true,
    list_present: true, list_visible: true, table_present: true, row_count: 0,
    empty_helper_present: false, empty_helper_visible: false, empty_help_text_length: 0,
    view_classes: ['o_list_view', 'o_renderer', 'PRIVATE-CLASS-PASSWORD'],
    native_columns: ['name', 'state', 'password'], visible_native_columns: ['name'],
    visible_menu_xmlids: ['portfolio_trade.menu_portfolio_sales', 'PRIVATE-MENU-TOKEN'],
    body: 'PRIVATE-BODY-SECRET', password: 'PRIVATE-PASSWORD-SECRET',
    input_values: ['PRIVATE-INPUT-SECRET'], cookie: 'PRIVATE-COOKIE-SECRET',
    expression: 'PRIVATE-EXPRESSION-SECRET', empty_help_text: 'PRIVATE-HELP-SECRET'});
  assert.equal(safe.row_count, 0);
  assert.equal(safe.list_visible, true);
  assert.deepEqual(safe.view_classes, ['o_list_view', 'o_renderer']);
  assert.deepEqual(safe.native_columns, ['name', 'state']);
  assert.deepEqual(safe.visible_menu_xmlids, ['portfolio_trade.menu_portfolio_sales']);
  for (const forbidden of ['private-', 'password', 'cookie', 'expression', 'input_values', 'body']) {
    assert.ok(!JSON.stringify(safe).toLowerCase().includes(forbidden));
  }
  const invalid = verifier.sanitizeDiagnostics({row_count: -1, empty_help_text_length: 'PRIVATE-COUNT',
    list_visible: 'PRIVATE-BOOLEAN', view_classes: Array(1000).fill('o_list_view')});
  assert.equal(Object.hasOwn(invalid, 'row_count'), false);
  assert.equal(Object.hasOwn(invalid, 'empty_help_text_length'), false);
  assert.equal(Object.hasOwn(invalid, 'list_visible'), false);
  assert.ok(invalid.view_classes.length <= 20);
});

test('Chrome startup diagnostics classify bounded stderr without exposing private text paths or environment', () => {
  const verifier = api();
  assert.equal(typeof verifier.chromeStartupDiagnostics, 'function', 'Safe Chrome startup diagnostics are missing');
  const metadata = {stderr: '[ERROR:process_singleton_posix.cc] Failed to bind() /home/PRIVATE-USER/PRIVATE-TOKEN/SingletonSocket: File name too long (36) PRIVATE-PASSWORD',
    binary: '/home/PRIVATE-USER/bin/google-chrome', exit_code: 21, signal: null,
    duration_ms: 15125, port_file_present: false, tmpdir_exists: true, debugging_loopback: true,
    environment: {PRIVATE_SECRET: 'PRIVATE-ENV'}, arguments: ['PRIVATE-ARGUMENT']};
  const safe = verifier.chromeStartupDiagnostics(metadata);
  assert.equal(safe.cause, 'singleton_socket_path_too_long');
  assert.equal(safe.binary_basename, 'google-chrome');
  assert.equal(safe.exit_code, 21);
  assert.equal(safe.signal, null);
  assert.equal(safe.duration_ms, 15125);
  assert.equal(safe.port_file_present, false);
  assert.equal(safe.tmpdir_exists, true);
  assert.equal(safe.debugging_loopback, true);
  for (const [stderr, cause] of [
    ['No usable sandbox! PRIVATE-SECRET', 'sandbox'],
    ['Failed to create shared memory in /dev/shm PRIVATE-SECRET', 'shared_memory'],
    ['Permission denied PRIVATE-SECRET', 'permission'],
    ['error while loading shared libraries: PRIVATE-SECRET.so: cannot open shared object file', 'missing_binary_or_library'],
    ['Unknown startup failure PRIVATE-SECRET', 'unknown'],
  ]) assert.equal(verifier.chromeStartupDiagnostics({...metadata, stderr}).cause, cause);
  assert.equal(verifier.chromeStartupDiagnostics({...metadata, stderr: '', timed_out: true}).cause, 'timeout');
  const bounded = verifier.chromeStartupDiagnostics({...metadata,
    stderr: 'x'.repeat(16384) + metadata.stderr, binary: '/PRIVATE-PATH/PRIVATE-BINARY', signal: 'PRIVATE-SIGNAL'});
  assert.equal(bounded.cause, 'unknown');
  assert.equal(bounded.binary_basename, 'unrecognized');
  assert.equal(bounded.signal, null);
  for (const report of [safe, bounded]) {
    const serialized = JSON.stringify(report).toLowerCase();
    for (const forbidden of ['private-', '/home', 'stderr', 'environment', 'arguments', 'password', 'token']) {
      assert.ok(!serialized.includes(forbidden), `Unsafe startup diagnostic ${forbidden}`);
    }
  }
});

test('every role screenshot is a unique safe relative PNG under the chosen runtime output', () => {
  const verifier = api();
  for (const screenshot of ['../sales.png', '/tmp/sales.png', 'file:///tmp/sales.png',
    'sales.jpg', 'profiles/cookies.png', 'finance.png']) {
    const evidence = dom('sales');
    evidence.screenshot = screenshot;
    assert.throws(() => verifier.validateDomEvidence('sales', evidence, fixture()));
  }
});

test('complete browser report requires seven validated roles and emits only safe evidence', () => {
  const verifier = api();
  const inputs = fixture();
  const evidence = Object.fromEntries(ROLES.map(role => [role, dom(role, inputs)]));
  const report = verifier.validateBrowserReport(evidence, inputs);
  assert.equal(report.ok, true);
  assert.deepEqual(Object.keys(report.roles).sort(), [...ROLES].sort());
  assert.equal(new Set(Object.values(report.roles).map(role => role.screenshot)).size, 7);
  for (const forbidden of ['password', 'cookie', 'token', 'session_id', 'expression', 'private-', 'dsn']) {
    assert.ok(!JSON.stringify(report).toLowerCase().includes(forbidden), `Unsafe output key ${forbidden}`);
  }
  delete evidence.manager2;
  assert.throws(() => verifier.validateBrowserReport(evidence, inputs));
});
