import { useEffect, useMemo, useState } from 'react';
import { Loader2, LogOut, Settings, TestTube, Wallet } from 'lucide-react';
import Login from './components/Login';
import CsvUploader from './components/CsvUploader';
import ChatInterface from './components/ChatInterface';
import ProductList from './components/ProductList';
import Cart from './components/Cart';
import TestSuiteModal from './components/TestSuiteModal';
import { AppState, CartItem, ChatMessage, Product } from './types';
import { indexProducts } from './services/searchEngine';
import {
  ensureDbConnection,
  getCart,
  getChatHistory,
  getProducts,
  saveCart,
  saveChatHistory,
  saveProducts
} from './services/dbService';
import { sendMessage } from './services/geminiService';

function formatDate(date: Date | string) {
  const d = typeof date === 'string' ? new Date(date) : date;
  return d;
}

export default function App() {
  const [appState, setAppState] = useState<AppState>(AppState.LOGIN);
  const [products, setProducts] = useState<Product[]>([]);
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [hits, setHits] = useState<Product[]>([]);
  const [cart, setCart] = useState<CartItem[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [exchangeRate, setExchangeRate] = useState(1050);
  const [testModalOpen, setTestModalOpen] = useState(false);

  useEffect(() => {
    const bootstrap = async () => {
      await ensureDbConnection();
      const storedProducts = await getProducts();
      if (storedProducts.length > 0) {
        setProducts(storedProducts);
        setAppState(AppState.DASHBOARD);
      }
      const storedHistory = await getChatHistory();
      setChatHistory(storedHistory.map((m) => ({ ...m, timestamp: formatDate(m.timestamp) })));
      const storedCart = await getCart();
      if (storedCart) setCart(storedCart);
      // Obtener cotización de dólar al arrancar
      try {
        const response = await fetch('https://dolarapi.com/v1/dolares/oficial');
        if (response.ok) {
          const data = await response.json();
          if (data.venta) setExchangeRate(Math.round(Number(data.venta)));
        }
      } catch (error) {
        console.warn('No se pudo obtener la cotización, uso default', error);
      }
    };
    bootstrap();
  }, []);

  useEffect(() => {
    if (products.length === 0) return;
    const syncCatalog = async () => {
      setIsSyncing(true);
      try {
        await fetch('/api/catalog', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ newProducts: products })
        });
      } catch (error) {
        console.error('Error al sincronizar catálogo', error);
      } finally {
        setIsSyncing(false);
      }
    };
    syncCatalog();
  }, [products]);

  const handleUpload = async (newProducts: Product[]) => {
    const indexed = indexProducts(newProducts);
    setProducts(indexed);
    await saveProducts(indexed);
    setAppState(AppState.DASHBOARD);
  };

  const handleSend = async (message: string) => {
    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      text: message,
      timestamp: new Date()
    };
    const nextHistory = [...chatHistory, userMessage];
    setChatHistory(nextHistory);
    setIsSending(true);
    try {
      const response = await sendMessage(message, nextHistory, exchangeRate);
      const botMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'model',
        text: response.text,
        timestamp: new Date()
      };
      setChatHistory([...nextHistory, botMessage]);
      setHits(response.hits);
      await saveChatHistory([...nextHistory, botMessage]);
    } catch (error) {
      const errorMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'model',
        text: (error as Error).message,
        timestamp: new Date(),
        isError: true
      };
      setChatHistory([...nextHistory, errorMessage]);
    } finally {
      setIsSending(false);
    }
  };

  const handleAddToCart = (product: Product) => {
    setCart((prev) => {
      const existing = prev.find((p) => p.id === product.id);
      const updated = existing
        ? prev.map((p) => (p.id === product.id ? { ...p, quantity: p.quantity + 1 } : p))
        : [...prev, { ...product, quantity: 1 }];
      saveCart(updated);
      return updated;
    });
  };

  const handleUpdateCart = (items: CartItem[]) => {
    setCart(items);
    saveCart(items);
  };

  const resetApp = async () => {
    setAppState(AppState.LOGIN);
    setProducts([]);
    setChatHistory([]);
    setCart([]);
    setHits([]);
    await saveProducts([]);
    await saveChatHistory([]);
    await saveCart([]);
  };

  const exchangeLabel = useMemo(() => `Dólar Tercom: $${exchangeRate} ARS`, [exchangeRate]);

  if (appState === AppState.LOGIN) {
    return <Login onEnter={setAppState} />;
  }

  if (appState === AppState.UPLOAD) {
    return <CsvUploader onUpload={handleUpload} />;
  }

  return (
    <div className="min-h-screen bg-slate-100">
      <TestSuiteModal isOpen={testModalOpen} onClose={() => setTestModalOpen(false)} exchangeRate={exchangeRate} />
      <header className="sticky top-0 z-10 bg-white border-b border-slate-200 shadow-sm">
        <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-2xl bg-indigo-600 text-white flex items-center justify-center font-bold">F</div>
            <div>
              <div className="font-semibold text-slate-900">Fran 4.0</div>
              <div className="text-sm text-slate-500">Mayorista de motopartes</div>
            </div>
            {isSyncing && <Loader2 className="w-4 h-4 text-indigo-500 animate-spin" title="Sincronizando catálogo" />}
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2 bg-indigo-50 text-indigo-700 px-3 py-1 rounded-xl">
              <Wallet className="w-4 h-4" />
              <span className="text-sm font-semibold">{exchangeLabel}</span>
            </div>
            <button className="p-2 rounded-lg hover:bg-slate-100" onClick={() => setTestModalOpen(true)}>
              <TestTube className="w-5 h-5 text-indigo-500" />
            </button>
            <button className="p-2 rounded-lg hover:bg-slate-100">
              <Settings className="w-5 h-5 text-slate-500" />
            </button>
            <button className="p-2 rounded-lg bg-red-50 text-red-600 hover:bg-red-100" onClick={resetApp}>
              <LogOut className="w-5 h-5" />
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 py-6">
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
          <section className="lg:col-span-4 h-[calc(100vh-160px)]">
            <ChatInterface messages={chatHistory} hits={hits} onSend={handleSend} isSending={isSending} />
          </section>
          <section className="lg:col-span-5 h-[calc(100vh-160px)]">
            <ProductList products={products} onAdd={handleAddToCart} />
          </section>
          <section className="lg:col-span-3 h-[calc(100vh-160px)]">
            <Cart items={cart} onUpdate={handleUpdateCart} />
          </section>
        </div>
      </main>
    </div>
  );
}
