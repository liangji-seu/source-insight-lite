/** Optional future adapter. No provider is instantiated or contacted by this MVP. */
export interface AIProvider {
  readonly name: string;
  isConfigured(): boolean;
  explain(structuredContext: string, signal?: AbortSignal): Promise<string>;
}
