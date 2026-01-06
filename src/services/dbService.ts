import { CartItem, ChatMessage, Product } from '../types';

const DB_NAME = 'fran-tercom-db';
const DB_VERSION = 1;
const STORE_PRODUCTS = 'products';
const STORE_METADATA = 'metadata';
const STORE_CHAT = 'chat_history';

let dbPromise: Promise<IDBDatabase> | null = null;

function createDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_PRODUCTS)) {
        db.createObjectStore(STORE_PRODUCTS, { keyPath: 'id' });
      }
      if (!db.objectStoreNames.contains(STORE_METADATA)) {
        db.createObjectStore(STORE_METADATA, { keyPath: 'key' });
      }
      if (!db.objectStoreNames.contains(STORE_CHAT)) {
        db.createObjectStore(STORE_CHAT, { keyPath: 'id' });
      }
    };

    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      db.onclose = () => {
        // Reconexión automática ante cierres inesperados
        dbPromise = null;
      };
      resolve(db);
    };
  });
}

async function getDb(retry = 0): Promise<IDBDatabase> {
  if (!dbPromise) {
    dbPromise = createDatabase().catch((error) => {
      dbPromise = null;
      throw error;
    });
  }

  try {
    return await dbPromise;
  } catch (error) {
    if (retry < 3) {
      // Espera progresiva antes de reintentar
      await new Promise((resolve) => setTimeout(resolve, 300 * (retry + 1)));
      return getDb(retry + 1);
    }
    throw error;
  }
}

async function runTransaction<T>(storeName: string, mode: IDBTransactionMode, handler: (store: IDBObjectStore) => void): Promise<T> {
  const db = await getDb();

  return new Promise<T>((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const store = tx.objectStore(storeName);

    tx.oncomplete = () => resolve((tx as unknown as { result: T }).result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);

    const result = handler(store);
    (tx as unknown as { result: T }).result = result as T;
  });
}

export async function saveProducts(products: Product[]): Promise<void> {
  await runTransaction<void>(STORE_PRODUCTS, 'readwrite', (store) => {
    store.clear();
    for (const product of products) {
      store.put(product);
    }
  });
}

export async function getProducts(): Promise<Product[]> {
  return runTransaction<Product[]>(STORE_PRODUCTS, 'readonly', (store) => {
    return new Promise<Product[]>((resolve, reject) => {
      const request = store.getAll();
      request.onsuccess = () => resolve(request.result as Product[]);
      request.onerror = () => reject(request.error);
    });
  });
}

export async function saveMetadata(key: string, value: unknown): Promise<void> {
  await runTransaction<void>(STORE_METADATA, 'readwrite', (store) => {
    store.put({ key, value });
  });
}

export async function getMetadata<T>(key: string): Promise<T | null> {
  return runTransaction<T | null>(STORE_METADATA, 'readonly', (store) => {
    return new Promise<T | null>((resolve, reject) => {
      const request = store.get(key);
      request.onsuccess = () => {
        if (request.result) {
          resolve(request.result.value as T);
        } else {
          resolve(null);
        }
      };
      request.onerror = () => reject(request.error);
    });
  });
}

export async function saveChatHistory(messages: ChatMessage[]): Promise<void> {
  await runTransaction<void>(STORE_CHAT, 'readwrite', (store) => {
    store.clear();
    for (const message of messages) {
      store.put(message);
    }
  });
}

export async function getChatHistory(): Promise<ChatMessage[]> {
  return runTransaction<ChatMessage[]>(STORE_CHAT, 'readonly', (store) => {
    return new Promise<ChatMessage[]>((resolve, reject) => {
      const request = store.getAll();
      request.onsuccess = () => resolve(request.result as ChatMessage[]);
      request.onerror = () => reject(request.error);
    });
  });
}

export async function saveCart(cart: CartItem[]): Promise<void> {
  await saveMetadata('cart', cart);
}

export async function getCart(): Promise<CartItem[] | null> {
  return getMetadata<CartItem[]>('cart');
}

export async function ensureDbConnection(): Promise<boolean> {
  try {
    await getDb();
    return true;
  } catch (error) {
    console.error('Error al abrir IndexedDB', error);
    return false;
  }
}
