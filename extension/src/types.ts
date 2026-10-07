export type SymbolKind = 'module' | 'class' | 'method' | 'function' | 'member' | 'constant';
export type RelationshipType = 'imports' | 'inherits' | 'owns' | 'calls' | 'overrides' | 'contains';
export type Direction = 'in' | 'out' | 'both';

export interface SymbolRecord {
  id: string;
  kind: SymbolKind;
  name: string;
  qualifiedName: string;
  file: string;
  startLine: number;
  endLine: number;
  parentId: string | null;
  moduleId: string | null;
  details: Record<string, unknown>;
}

export interface Relationship {
  sourceId: string;
  targetId: string | null;
  type: RelationshipType;
  status: 'resolved' | 'inferred' | 'unresolved' | 'dynamic';
  confidence: 'high' | 'medium' | 'low';
  label?: string;
  file?: string;
  line?: number;
}

export interface IndexError { file: string; message: string; }
export interface IndexResult {
  root: string;
  files: number;
  symbols: number;
  relationships: number;
  errors: IndexError[];
  elapsedMs: number;
}
export interface Summary extends IndexResult { indexedAt?: string; }
export interface InspectResult {
  symbol: SymbolRecord;
  children: SymbolRecord[];
  outgoing: Relationship[];
  incoming: Relationship[];
  related: SymbolRecord[];
}
export interface GraphResult {
  nodes: SymbolRecord[];
  edges: Relationship[];
  truncated: boolean;
}
export interface ExplainContext { symbol: SymbolRecord; context: string; }
export interface ProgressEvent { completed: number; total: number; file?: string; }

export interface EngineRequestMap {
  index: { root: string; excludes?: string[] };
  summary: Record<string, never>;
  symbols: { kind?: SymbolKind; parentId?: string; query?: string; file?: string; limit?: number; offset?: number };
  inspect: { id: string };
  context: { file: string; line: number };
  graph: { id: string; types?: RelationshipType[]; direction?: Direction; depth?: number; limit?: number };
  moduleGraph: { id: string; depth?: number; limit?: number };
  classGraph: { id?: string; file?: string; depth?: number; limit?: number };
  explainContext: { id: string };
  shutdown: Record<string, never>;
}
export interface EngineResultMap {
  index: IndexResult;
  summary: Summary | null;
  symbols: SymbolRecord[];
  inspect: InspectResult;
  context: SymbolRecord | null;
  graph: GraphResult;
  moduleGraph: GraphResult;
  classGraph: GraphResult;
  explainContext: ExplainContext;
  shutdown: true;
}
