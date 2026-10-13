// Browser-local persistence. Non-exportable keys still require a trusted browser profile.
window.connectionStore = (() => {
  let database;
  let queue = Promise.resolve();
  function db() {
    if (!globalThis.crypto?.subtle || !globalThis.indexedDB) {
      return Promise.reject(new Error("当前环境不支持加密保存，请使用 localhost 或 HTTPS"));
    }
    if (!database) database = new Promise((resolve, reject) => {
      const request = indexedDB.open("dsm-connections", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("connection");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(new Error("浏览器不允许保存连接信息"));
    });
    return database;
  }
  async function read() {
    const database = await db();
    return new Promise((resolve, reject) => {
      const request = database.transaction("connection").objectStore("connection").get("last");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(new Error("读取连接信息失败"));
    });
  }
  async function write(record) {
    const database = await db();
    await new Promise((resolve, reject) => {
      const transaction = database.transaction("connection", "readwrite");
      transaction.objectStore("connection").put(record, "last");
      transaction.oncomplete = resolve;
      transaction.onabort = transaction.onerror = () => reject(new Error("保存连接信息失败"));
    });
  }
  return {
    async load() {
      await queue;
      const record = await read();
      if (!record) return null;
      const decoded = await crypto.subtle.decrypt({ name: "AES-GCM", iv: record.iv }, record.key, record.data);
      return JSON.parse(new TextDecoder().decode(decoded));
    },
    save(credentials) {
      const snapshot = JSON.stringify(credentials);
      const next = queue.then(async () => {
        const existing = await read();
        const key = existing?.key || await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const data = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(snapshot));
        await write({ key, iv, data });
      });
      queue = next.catch(() => {});
      return next;
    },
  };
})();
