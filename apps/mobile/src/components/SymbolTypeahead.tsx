/**
 * SymbolTypeahead — the mobile ticker-add input.
 *
 * Owns the combobox state (`useTickerCombobox`) and renders the text
 * field, the Add button, and the suggestion list as one self-contained
 * unit. It knows nothing about watchlist validation or the add mutation —
 * it only hands a committed string upward via `onCommitSymbol`, whether
 * that string came from picking a suggestion or from typing an exact
 * ticker and submitting without ever opening the dropdown. The caller
 * decides whether that string is valid, runs the mutation, and — only
 * once it actually succeeds — clears this component by changing its
 * `key`, which remounts it with a fresh, empty combobox. That mirrors the
 * desktop rule: don't clear what the user typed until we know it worked.
 */

import { TextInput, View } from 'react-native';

import { BentoCTA } from '@/components/bento';
import { useTickerCombobox } from '@/hooks/useTickerCombobox';

import { SymbolResultsList } from './SymbolResultsList';

export function SymbolTypeahead({
  onCommitSymbol,
  isPending = false,
}: {
  onCommitSymbol: (symbol: string) => void;
  isPending?: boolean;
}) {
  const { query, setQuery, open, close, hits } = useTickerCombobox(8);

  /** Fires for both a dropdown pick and the type-exact-ticker fallback. */
  const commit = (raw: string) => {
    const value = raw.trim().toUpperCase();
    if (!value) return;
    close();
    onCommitSymbol(value);
  };

  return (
    <View className="relative flex-row items-center gap-2">
      <TextInput
        value={query}
        onChangeText={setQuery}
        onSubmitEditing={() => commit(query)}
        autoCorrect={false}
        maxLength={40}
        placeholder="Search ticker or company"
        accessibilityLabel="Search for a ticker or company to add to the watchlist"
        className="min-h-[44px] flex-1 rounded-lg border border-hairline px-3 text-[15px] text-text-primary dark:border-hairline-dark dark:text-text-primary-dark"
        style={{ fontVariant: ['tabular-nums'] }}
      />
      <View className="w-28">
        <BentoCTA
          label={isPending ? 'Adding…' : 'Add'}
          onPress={() => commit(query)}
          disabled={isPending || query.trim().length === 0}
          accessibilityLabel={`Add ${query.trim().toUpperCase() || 'symbol'} to the watchlist`}
        />
      </View>
      {open && hits.length > 0 ? (
        <View className="absolute left-0 right-0 top-[52px] z-10 shadow-lg">
          <SymbolResultsList hits={hits} onSelect={(hit) => commit(hit.symbol)} />
        </View>
      ) : null}
    </View>
  );
}
