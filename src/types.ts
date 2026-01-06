export interface Product {
  id: string;
  code: string;
  name: string;
  price: number;
  currency: 'ARS' | 'USD';
  category: string;
  stock?: number;
  _normName?: string;
  _normCode?: string;
  _tokens?: string[];
  _ngrams?: string[];
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'model' | 'system';
  text: string;
  timestamp: Date;
  isError?: boolean;
}

export enum AppState {
  LOGIN = 'LOGIN',
  UPLOAD = 'UPLOAD',
  DASHBOARD = 'DASHBOARD'
}

export interface CartItem extends Product {
  quantity: number;
}
