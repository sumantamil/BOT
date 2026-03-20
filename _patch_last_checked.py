with open(r'd:\Users\sundlnu\VS Code BOT\BOT\bot\engine.py', encoding='utf-8') as f:
    lines = f.readlines()

eod_line   = next(i for i,l in enumerate(lines) if 'await self._check_eod_signal()' in l)
trend_line = next(i for i,l in enumerate(lines) if 'signal = self.analyzer.analyze(regime=regime_value)' in l)

# Insert EOD first (higher line number) so TREND insertion index stays valid
lines.insert(eod_line,   '                    _last_checked["EOD"] = datetime.now()\n')
lines.insert(trend_line, '                    _last_checked["TREND"] = datetime.now()\n')

with open(r'd:\Users\sundlnu\VS Code BOT\BOT\bot\engine.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print("Patched OK: TREND at line", trend_line+1, "EOD at line", eod_line+2)
