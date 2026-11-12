# codelib 鈥?浠ｇ爜闂瓟鏈哄櫒浜猴紙鏋佺畝鐗堬級

鎶婁唬鐮佺洰褰曟帴鎴愬彲闂瓟鐨勬満鍣ㄤ汉锛氬唴缃?*缃戦〉瀵硅瘽鐣岄潰**锛屾敮鎸佸涓煡璇嗗簱銆佺鐞嗗憳涓庢櫘閫氳处鍙凤紱涔熸敮鎸?*浼佷笟寰俊 / 椋炰功**鎺ュ叆涓庢櫘閫?HTTP 璋冪敤銆傚唴缃?*澧為噺绱㈠紩銆佸彉鏇存劅鐭ャ€侀暱鏈熻蹇嗐€佺渷 token 妫€绱?*锛屽鐩爣宸ョ▼**鍙**銆?
璁捐鍘熷垯锛?*閰嶇疆灏戙€佹帴鍙ｅ皯**銆傚ぇ妯″瀷鍙厤涓€涓紱鏃ュ父绠＄悊璧板璇濆懡浠ゃ€?
- Python 3.10+ / FastAPI
- 娌℃湁澶фā鍨嬩篃鑳界敤锛氭湭閰嶇疆鏃惰嚜鍔ㄩ檷绾т负銆屾绱㈡憳瑕併€嶄綔绛?
---

## 1. Web 宸ヤ綔鍙帮紙鍐呯綉 路 澶氬簱 路 鍙锛?
娴忚鍣ㄦ墦寮€ `http://<鍐呯綉IP>:8000/`锛?
- 宸︿晶**鐭ヨ瘑搴撳垪琛?*锛氭樉绀烘瘡涓簱鏄惁宸插缓搴撱€佸潡鏁?绗﹀彿鏁帮紱鐐归€夊嵆鍦ㄨ搴撲笅鏂板缓瀵硅瘽锛?- **姣忎釜瀵硅瘽鍥哄畾缁戝畾涓€涓簱**锛屾彁闂彧鍦ㄨ搴撳唴妫€绱紝涓嶄細涓插簱锛?- AI 鍥炵瓟鏀寔 Markdown 鏍囬銆佸垪琛ㄣ€佽〃鏍笺€佸紩鐢ㄥ拰浠ｇ爜鍧楋紱瑙ｆ瀽鍣ㄩ殢鏈嶅姟鏈湴鎻愪緵锛屽伐浣滃彴涓嶄緷璧栧缃戝姞杞斤紱
- **鍘嗗彶瀵硅瘽**鎸夋椂闂村垪鍑猴紝鍙洖鐪嬨€佺户缁拷闂紱
- 椤甸潰椤堕儴涓庝晶鏍忔爣娉ㄣ€岎煍?鍙妯″紡銆嶏細**鏈嶅姟娌℃湁浠讳綍淇敼宸ョ▼鏂囦欢鐨勬帴鍙?*銆?
### 鍙鏄€庝箞淇濊瘉鐨勶紙涓夐噸锛?
1. **浠ｇ爜灞?*锛氭湰鏈嶅姟鍙湁鈥滆鈥濊兘鍔涳紙tree-sitter 瑙ｆ瀽銆丗TS5 妫€绱€丩LM 鐢熸垚锛夛紝鏃犲啓鏂囦欢/鏀逛唬鐮?鎵ц鍛戒护鐨勫伐鍏凤紱
2. **鎺ュ彛灞?*锛歚app.read_only: true` 鏃讹紝`/api/write`銆乣/api/edit`銆乣/api/apply`銆乣/api/git`銆乣/fs/*` 绛夎矾寰勮涓棿浠剁粺涓€ **403**锛?3. **鎻愮ず灞?*锛氱郴缁熸彁绀鸿姹傛ā鍨嬪彧缁欒В閲?缁撹/鏂规锛堝彲鎻忚堪鏀瑰摢閲屻€佸浣曢獙璇侊級锛屼笉浜у嚭鍙墽琛岃ˉ涓併€佷笉澹扮О宸叉敼鏂囦欢銆?
> 鏇磋繘涓€姝ワ細鍙妸 `workspace.path` 浠?*鍙鏂瑰紡**鎸傝浇/鎺堟潈锛堝 NTFS 鍙粰璇诲彇鏉冮檺锛夛紝鍙屼繚闄┿€?
### 鍐呯綉閮ㄧ讲锛圵indows 鍗曟湇鍔★級

```bat
:: 1) 瀹夎渚濊禆锛堜竴娆★級
python -m pip install -r requirements.txt -r requirements-knowledge.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 2) 涓烘瘡涓簱寤虹储寮曪紙棣栨/澧為噺锛?python -m app kb-build-all
:: 鎴栧崟鐙細python -m app kb-build --workspace gateway --full

:: 3) 鍚姩鏈嶅姟锛堝墠鍙帮級
python -m app serve --port 8000
:: 鍚姩鏃朵細鎵撳嵃锛氭湰鏈鸿闂?http://127.0.0.1:8000/  鍐呯綉璁块棶 http://<鍐呯綉IP>:8000/
```

鍐呯綉鍏跺畠鐢佃剳璁块棶鍓嶏紝鏀捐绔彛锛堢鐞嗗憳 PowerShell锛屽彧闇€涓€娆★級锛?
```powershell
New-NetFirewallRule -DisplayName "codelib 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

濡傞渶寮€鏈鸿嚜鍚紝鍙敤 NSSM 鎴栥€屼换鍔¤鍒掔▼搴忋€嶆妸 `python -m app --config config/config.yaml serve --port 8000` 娉ㄥ唽涓烘湇鍔°€?
Windows 涔熷彲浠ュ弻鍑讳粨搴撴牴鐩綍鐨?`start.bat`锛屾垨鍦?PowerShell 杩愯 `./start.ps1`銆傞娆″惎鍔ㄤ笖璐﹀彿搴撲负绌烘椂锛岃剼鏈粯璁ゅ垱寤虹敤鎴峰悕 `admin`锛屽苟瀹夊叏鎻愮ず杈撳叆鑷冲皯 8 浣嶇殑绠＄悊鍛樺瘑鐮侊紱瀵嗙爜鍙啓鍏?SQLite 鐨勫搱甯岋紝涓嶄繚瀛樺湪鑴氭湰涓€傚凡鏈夎处鍙峰悗浼氱洿鎺ュ惎鍔紝涓嶅啀鎻愮ず鎴栬鐩栧瘑鐮併€傞渶瑕佽嚜瀹氫箟鍒濆鐢ㄦ埛鍚嶆椂锛屽惎鍔ㄥ墠璁剧疆鐜鍙橀噺 `CODELIB_ADMIN_USERNAME`銆?
### 鐭ヨ瘑搴撲笌瀵硅瘽绠＄悊

绠＄悊鍛樼櫥褰曞伐浣滃彴锛屾墦寮€銆岃处鍙?鐭ヨ瘑搴撶鐞嗐€嶅嵆鍙細

- 鏌ョ湅姣忎釜搴撶殑璺緞銆佹潵婧愩€佺储寮曠増鏈€佹枃浠?鏂囨。鍧?绗﹀彿/鍥捐竟缁熻锛?- 鍒涘缓搴擄紝缂栬緫鍚嶇О銆佺洰褰曚笌璇存槑锛屾墽琛屽閲忔垨鍏ㄩ噺閲嶅缓锛?- 鍒犻櫎搴撳強鍏舵巿鏉冦€佸璇濄€佽蹇嗐€佺暀鐥曞拰绱㈠紩锛屼絾涓嶄細鍒犻櫎婧愬伐绋嬫枃浠讹紱
- 缁欐櫘閫氳处鍙峰垎閰嶅簱鏉冮檺锛屽苟鏌ョ湅銆佹墦寮€銆侀噸鍛藉悕鎴栧垹闄ゅ璇濄€?
閰嶇疆搴撲笌鍔ㄦ€佸簱閮藉彲绠＄悊銆傚閰嶇疆搴撶殑淇敼鍜屽垹闄や細浣滀负瑕嗙洊璁板綍鎸佷箙鍖栧埌 SQLite锛岄噸鍚悗浠嶇劧鐢熸晥锛涘垹闄ゅ悗鍙噸鏂板垱寤哄悓 ID 鐨勫簱銆傛柊寤哄簱涓嶅繀鏀归厤缃枃浠躲€?
鍛戒护琛屽缓搴撲粛鍙敤浜庨厤缃枃浠堕噷宸插畾涔夌殑搴擄細

```bash
python -m app kb-build --workspace <鏂癷d>
```

鍒锋柊缃戦〉锛屽乏渚у氨浼氬嚭鐜版柊搴撱€?*姣忎釜搴撳搴斾竴涓伐绋嬬洰褰曪紝褰兼闅旂銆?*

### HTTP 鎺ュ彛

| 鏂规硶 | 璺緞 | 鐢ㄩ€?|
|------|------|------|
| GET | `/` | Web 宸ヤ綔鍙?|
| GET | `/healthz` | 鍋ュ悍妫€鏌ワ紙鍙鐘舵€佷笌鐭ヨ瘑搴撴暟閲忥級 |
| POST | `/api/auth/login` | 鐢ㄦ埛鍚?瀵嗙爜鐧诲綍锛屽缓绔?HttpOnly 浼氳瘽 |
| POST | `/api/auth/logout` | 娉ㄩ攢褰撳墠浼氳瘽 |
| GET | `/api/auth/me` | 褰撳墠鐧诲綍璐﹀彿 |
| GET | `/api/workspaces` | 褰撳墠璐﹀彿鍙闂殑搴撳垪琛?|
| GET | `/api/admin/workspaces` | 绠＄悊鍛樻煡鐪嬪叏閮ㄧ煡璇嗗簱 |
| POST | `/api/admin/workspaces` | 绠＄悊鍛樺垱寤哄苟绱㈠紩鐭ヨ瘑搴?`{name,id?,path,description?}` |
| PUT | `/api/admin/workspaces/{id}` | 缂栬緫鍚嶇О銆佺洰褰曞拰璇存槑锛涚洰褰曞彉鍖栨椂鑷姩鍏ㄩ噺閲嶅缓 |
| POST | `/api/admin/workspaces/{id}/rebuild` | 澧為噺/鍏ㄩ噺閲嶅缓 `{full}` |
| DELETE | `/api/admin/workspaces/{id}` | 鍒犻櫎搴撳強鍏舵巿鏉冦€佸璇濄€佽蹇嗐€佺暀鐥曞拰绱㈠紩锛屼笉鍒犳簮宸ョ▼ |
| GET/POST | `/api/admin/users` | 绠＄悊鍛樻煡鐪?鍒涘缓璐﹀彿 |
| PUT | `/api/admin/users/{id}/workspaces` | 绠＄悊鍛樿缃櫘閫氳处鍙峰彲璁块棶鐨勭煡璇嗗簱 |
| PUT | `/api/admin/users/{id}/password` | 绠＄悊鍛橀噸缃处鍙峰瘑鐮佸苟娉ㄩ攢鏃т細璇?|
| PATCH | `/api/admin/users/{id}/active` | 绠＄悊鍛樺惎鐢?鍋滅敤璐﹀彿 |
| GET | `/api/knowledge/status` | 绠＄悊鍛樻煡鐪嬪悇搴撶煡璇嗗簱缁熻 |
| POST | `/api/conversations` | 鏂板缓瀵硅瘽 `{workspace}` |
| GET | `/api/conversations` | 鍘嗗彶瀵硅瘽鍒楄〃 |
| GET | `/api/conversations/{id}` | 鏌愬璇濆叏閮ㄦ秷鎭?|
| POST | `/api/conversations/{id}/messages` | 鍦ㄨ瀵硅瘽鎻愰棶 `{content}` |
| PATCH | `/api/conversations/{id}` | 閲嶅懡鍚嶅璇?`{title}` |
| DELETE | `/api/conversations/{id}` | 鍒犻櫎瀵硅瘽 |
| POST | `/ask` | 鐧诲綍鍚庡崟娆￠棶绛旓紙渚涚▼搴忚皟鐢級 |
| GET/POST | `/channels/wecom/callback` | 浼佷笟寰俊 |
| POST | `/channels/feishu/event` | 椋炰功 |

### 鎬庝箞鐢?
1. 鍦?`config/config.yaml` 閰嶅ソ `llm`锛沇indows 棣栨鍚姩鍙繍琛?`start.bat`锛屽叾浠栫幆澧冨垯璁剧疆 `CODELIB_ADMIN_USERNAME` 鍜屽己瀵嗙爜 `CODELIB_ADMIN_PASSWORD`锛?2. 鐢?bootstrap 璐﹀彿鐧诲綍宸ヤ綔鍙帮紱
3. 绠＄悊鍛樺垱寤虹煡璇嗗簱銆佸垱寤烘櫘閫氳处鍙峰苟鍒嗛厤鐭ヨ瘑搴擄紝鏅€氳处鍙风櫥褰曞悗鍙兘闂瓟宸叉巿鏉冨簱銆?
```bash
curl -s -c cookies.txt http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"浣犵殑绠＄悊鍛樺瘑鐮乗"}"

curl -s -b cookies.txt http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"杩欎釜宸ョ▼鏄仛浠€涔堢殑锛焅"}"
```

杩斿洖寰堝共鍑€锛屽彧鏈夌瓟妗堝拰鏉ユ簮锛?
```json
{
  "answer": "tdx_msg_bus_svr 鏄粺涓€娑堟伅缃戝叧锛岃礋璐ｅ睆钄?QD/IDeal 娓犻亾宸紓锛屽悜涓婃父鎻愪緵缁熶竴鐨勬秷鎭彂閫併€佹煡璇笌涓婅浜嬩欢鑳藉姏銆?,
  "sources": ["docs/01-涓氬姟涓庢灦鏋?娑堟伅缃戝叧-涓氬姟姒傝.md:1-4", "docs/01-涓氬姟涓庢灦鏋?娑堟伅缃戝叧-涓氬姟姒傝.md:20-28"]
}
```

- `workspace` 鍙渷鐣ワ紙鍗曠洰褰曟椂锛夛紱浼?`id` / `name` / 鐩綍鍚嶉兘鑳藉尮閰嶃€?- 鎯崇湅寮曠敤璇勫垎銆佽蹇嗗懡涓€乼oken 鐢ㄩ噺绛夎瘖鏂俊鎭紝璇锋眰浣撳姞 `"debug": true`锛岃瘖鏂俊鎭湪 `debug` 瀛楁閲屻€?- 寰俊 / 椋炰功閲岀洿鎺ョ敤鑷劧璇█鍙戞秷鎭嵆鍙紝涓嶇敤鍏冲績杩欎簺瀛楁銆?- 榛樿姝ｆ枃浠ヨ嚜鐒惰瑷€缁撹涓轰富锛屼笉鍫嗙爩璺緞銆佽鍙峰拰鏉ユ簮娓呭崟锛涚綉椤典腑鐨勫弬鑰冭祫鏂欓粯璁ゆ姌鍙狅紝娓犻亾涓嶅啀棰濆杩藉姞鏉ユ簮銆?- 闇€瑕佸畾浣嶆椂鍙銆岀粰鍑鸿鍙锋潵婧愩€嶏紝姝ｆ枃鏈€澶氫繚鐣欏皯閲忓叧閿紩鐢紱璇淬€屼笉瑕佷唬鐮併€佸嚱鏁板悕鍜屽瓧娈靛悕銆嶆椂浼氭敼鐢ㄧ函鑷劧璇█锛屽苟鍦ㄥ綋鍓嶅璇濈殑鍚庣画杩介棶涓欢缁鍋忓ソ銆?
> 绋嬪簭璋冪敤鍙鐢ㄧ櫥褰?cookie锛屾垨閫氳繃 `Authorization: Bearer <session-token>` 浼犻€?API 浼氳瘽銆傚瘑鐮佸彧淇濆瓨 PBKDF2 鍝堝笇锛涘垵濮嬬鐞嗗憳鍙湪鏁版嵁搴撻娆″垱寤烘椂浠庣幆澧冨彉閲忓紩瀵笺€傛櫘閫氳处鍙峰彧鑳界湅鍒板拰鎻愰棶绠＄悊鍛樺凡鍒嗛厤鐨勭煡璇嗗簱锛屽苟涓斿彧鑳芥煡鐪嬭嚜宸辩殑瀵硅瘽锛涘仠鐢ㄦ垨閲嶇疆瀵嗙爜浼氱珛鍗虫敞閿€璇ヨ处鍙风幇鏈変細璇濄€?
### 浠ｇ爜鐭ヨ瘑搴擄紙P0锛屽凡鍐呯疆锛?
鍦ㄥ師鏈夆€滅鍚嶅潡鈥濅箣澶栵紝鏂板 **浠ｇ爜鐭ヨ瘑宸ョ▼灞?*锛堣繘绋嬪唴銆佸崟 SQLite 鏂囦欢銆佹棤瀹瑰櫒/鏃犳湰鍦版ā鍨嬶級锛?
- 鐭ヨ瘑缁勭粐锛氭枃浠剁骇缁撴瀯鎽樿 + 鍑芥暟/绫?鏂规硶绮剧‘琛屽彿锛涢暱鍑芥暟涓庨暱鏂囨。鑷€傚簲鍒嗘锛岄伩鍏嶅彧淇濈暀寮€澶?- 浠ｇ爜锛?*tree-sitter** 鎶界鍙?+ 绮剧‘鏍囪瘑绗﹂偦鎺ヨ竟锛堣皟鐢?寮曠敤浠嶄负鍚彂寮忥級
- 鏂囨。锛?*markitdown** + python-docx/pptx/openpyxl + pypdf锛圥DF/Word/PPT/Excel/MD锛?- 妫€绱細**SQLite FTS5锛圔M25锛? 涓嫳鍚屼箟璇?+ 澶氬瓙鍙ヨ鐩栬瀺鍚?*锛屽彲閫?LLM 閲嶆帓涓庢渶澶氫笁璺抽偦鎺ユ墿灞?- 鐢熸垚锛氫粛璧颁綘鐨?LLM API锛堢幇鏈?`3425` 缃戝叧锛?
```bash
python -m pip install -r requirements-knowledge.txt

python -m app kb-build --workspace gateway        # 棣栨/澧為噺寤哄簱锛堣嚜鍔ㄨ烦杩囨湭鍙樻枃浠讹級
python -m app kb-build --workspace gateway --full # 鍏ㄩ噺閲嶅缓
python -m app kb-stats                            # 鐪嬬粺璁?```

寤哄簱鍚?`bot` 浼?*鑷姩浼樺厛浣跨敤鐭ヨ瘑搴?*锛堝簱涓虹┖鏃惰嚜鍔ㄥ洖閫€鏃ф绱級锛屾棤闇€鏀归厤缃€傞粯璁ゅ惎鐢?LLM 閲嶆帓锛涘鏉傞棶棰樹細鐢熸垚缁撴瀯鍖栧瓙鏌ヨ锛屽垎鏋愮被闂鎸夌己鍙ｈˉ妫€绱€佷慨璁㈠苟澶嶆牳鍏抽敭鏂█鐨勮瘉鎹€俙!deep` 鑷冲皯鎵╁睍涓よ烦骞跺厑璁告渶澶氫笁杞慨璁紝鍥犳浼氬鍔犳ā鍨嬭皟鐢ㄥ拰鍝嶅簲鏃堕棿銆?
> 寤哄簱鏍煎紡宸插崌绾э細鏃у簱涓嬫鎵ц `kb-build` 浼氳嚜鍔ㄥ叏閲忛噸寤猴紝鏃犻渶鎵嬪伐鍒犲簱銆傚綋鍓嶆绱粛鏄瘝娉曟绱笌鍚屼箟璇嶆墿灞曪紝灏氭湭瀹炵幇鍚戦噺绱㈠紩锛沗knowledge.embedding.mode` 鐩墠涓嶄細鍚敤娣峰悎妫€绱€?
---

### 鍏ㄩ摼璺暀鐥曚笌杩囩▼璇勪及锛堟函婧愶級

**姣忎竴娆￠棶绛旈兘浼氳嚜鍔ㄧ暀鐥?*锛屽畬鏁磋褰?7 涓樁娈碉細

| 闃舵 | 璁板綍鍐呭 |
|------|----------|
| `sanitized` | 鐢ㄦ埛鍘熷鎻愰棶锛堝師璇?+ 娓呮礂鍚庯級 |
| `intent` | 澶氳疆鐞嗚В/鏀瑰啓缁撴灉銆佸巻鍙茶疆鏁般€佹敼鍐欐柟寮忥紙llm/heuristic锛?|
| `index` | 绱㈠紩/鍙樻洿妫€娴嬫儏鍐?|
| `retrieve` | 妫€绱㈡潵婧愩€佸€欓€変笌閫変腑鐗囨锛坧ath:line锛夈€佷笂涓嬫枃 token銆侀绠?|
| `evidence` | 涓嫳鏂囦富棰樺尮閰嶇殑鍛戒腑璇嶃€佺己澶辫瘝鍙婁綔涓鸿寖鍥村拷鐣ョ殑宸ョ▼鍚?|
| `prompt` | **瀹為檯鍙戠粰澶фā鍨嬬殑鍗忚鍐呭**锛坰ystem + user 鍏ㄦ枃銆佸悇鑷?token 鏁帮級 |
| `llm` | 妯″瀷鍘熷杈撳嚭銆乫inish_reason銆乽sage锛坧rompt/completion tokens锛夈€佽€楁椂 |
| `quality_review` | 鍒嗘瀽绫婚棶棰樼殑瑕嗙洊妫€鏌ョ粨鏋滐紱鏈夋槑纭己鍙ｆ椂鏍囪鏄惁宸蹭慨璁?|
| `answer` | 鏈€缁堢瓟妗?+ 寮曠敤鏉ユ簮 |

**杩囩▼璇勪及**锛堝浠绘剰涓€娆＄暀鐥曪紝涓€閿瘎浼帮級锛氬垽鏂姹備笌鍥炵瓟鏄惁鍚堢悊锛屽苟缁欏嚭 harness 鏀硅繘寤鸿銆?
- **瑙勫垯妫€鏌?*锛堢‘瀹氭€с€侀浂鎴愭湰锛夛細妫€绱㈡槸鍚﹀懡涓€佷笂涓嬫枃鏄惁鎺ヨ繎棰勭畻琚埅鏂€佹ā鍨嬫槸鍚﹀け璐?闄嶇骇銆佸洖绛旀槸鍚︿负绌?杩囩煭銆佹槸鍚﹀甫寮曠敤銆?*寮曠敤鏄惁鍙拷婧?*锛堝洖绛旈噷鎻愬埌鐨勬枃浠舵槸鍚︾湡鍦ㄦ绱㈢粨鏋滀腑锛夈€佹彁绀鸿瘝鏄惁瀹屾暣锛?- **妯″瀷璇勫**锛? 娆¤皟鐢級锛氬繝瀹炲害/鐩稿叧鎬?瀹屾暣鎬?鏄惁璺戦 + 0-100 鍒嗭紱
- **harness 鍙彁鍗囩偣**锛氬鈥滄煇鐗囨琚埅鏂簲鎵╁ぇ琛屾暟鈥濃€渃onfig 鍖呮湭绾冲叆妫€绱⑩€濃€滃簲鍙洖鏌愭枃浠垛€濃€滃缓璁紑鍚噸鎺?鎻愰珮 top_k鈥濓紝鍙惤鍦伴€愭潯缁欏嚭銆?
缃戦〉涓婄偣鍙充笂銆屾函婧?璇勪及銆嶅嵆鍙煡鐪嬩换鎰忎竴娆¤皟鐢ㄧ殑鍏ㄩ摼璺紝骞剁偣銆岃瘎浼板綋鍓嶃€嶇敓鎴愭姤鍛娿€傛帴鍙ｏ細

| 鏂规硶 | 璺緞 | 鐢ㄩ€?|
|------|------|------|
| GET | `/api/traces?workspace=&limit=` | 鐣欑棔鍒楄〃锛堝惈鏈€杩戣瘎浼扮粨璁猴級 |
| GET | `/api/traces/{trace_id}` | 鍗曟鐣欑棔鍏ㄨ矊锛堝惈鎵€鏈夐樁娈典笌鍘嗘璇勪及锛?|
| POST | `/api/traces/{trace_id}/evaluate` | 杩愯璇勪及锛堣鍒?妯″瀷锛夛紝缁撴灉钀藉簱 |
| GET | `/api/traces/{trace_id}/audits` | 鍘嗘璇勪及璁板綍 |

> 鐣欑棔涓庤瘎浼伴兘瀛樻湰鍦?SQLite锛坄trace` / `trace_event` / `audit` 琛級锛屼笉渚濊禆澶栭儴鏈嶅姟銆?
### 璇勪及闂幆锛氱粨璁鸿嚜鍔ㄦ矇娣€涓衡€滃彫鍥炴彁绀衡€?
璇勪及閲岀殑鈥滃簲鍙洖鏌愭枃浠?绗﹀彿/鐩綍鈥濅笉浼氳瀹屽氨涓⑩€斺€旂偣銆岃瘎浼板綋鍓嶃€嶆椂锛堟帴鍙?`?learn=true` 榛樿寮€鍚級锛岀郴缁熶細鑷姩锛?
1. 浠庤瘎浼版姤鍛婇噷鎶藉彇**缂哄け绫?*寮曠敤锛堝彧璁も€滅己灏?搴斿彫鍥?寤鸿琛ュ厖/鏈撼鍏モ€濈瓑璇箟锛屾棤鍏抽」涓嶅叆锛夛紱
2. 鍐欏叆璇ュ簱鐨?`boost` 琛紙`kind` = path / dir / symbol锛屾潈閲嶉粯璁?脳1.5锛岄噸澶嶇疮鍔狅紝涓婇檺 脳5锛夛紱
3. **涓嬫妫€绱㈢洿鎺ョ敓鏁?*锛氬懡涓?boost 鐨勬枃浠?绗﹀彿鍊欓€夎鍔犳潈缃《銆?
鏁堟灉瀹炴祴锛堜綘鐨?`tdx_msg_bus_svr`锛夛細璇勪及鍚庡鍒?`config/kafka_config.go` 涓?`config/`锛?鍘熸湰杩欐潯鏍规湰娌¤鍙洖锛屽啀妫€绱㈡椂鐩存帴杩涘叆 **绗?2 浣嶃€佸懡涓?6 鏉″姞鏉?*銆?
绠＄悊鎺ュ彛锛?
| 鏂规硶 | 璺緞 | 鐢ㄩ€?|
|------|------|------|
| GET | `/api/boosts?workspace=` | 鏌ョ湅褰撳墠搴撶殑鍙洖鎻愮ず涓庢潈閲?|
| DELETE | `/api/boosts?kind=&pattern=&workspace=` | 鍒犻櫎鏌愭潯锛堥伩鍏嶈鍔犳潈锛?|

> 闂幆鏄?*鍙帶鍙€?*鐨勶細瑙勫垯鍙銆佸彲鍒狅紱涔熷彲鍦ㄨ瘎浼版帴鍙ｄ紶 `learn=false` 鍙瘎浼颁笉瀛︿範銆?
### 鍥炵瓟璐ㄩ噺鐨勪笁閬撻椄锛堜笉闈犺皟鏉冮噸锛?
璋冩潈閲嶅彧鑳芥敼鎺掑簭锛涗笅闈笁椤硅В鍐斥€滃€欓€夋湰韬笉瓒?/ 绛旀鏈韩涓嶅彲闈犫€濓細

1. **璇佹嵁闂ㄦ锛堜笉纭瓟锛?*锛氬悓鏃舵鏌ヤ腑鏂囦富棰樺拰浠ｇ爜鏍囪瘑绗︼紝宸ョ▼鍚嶅彧浣滀负妫€绱㈣寖鍥达紱鐢ㄦ埛鏄庣‘鎸囧畾鐨勫嚱鏁板悕浠嶉渶鍛戒腑銆傝瘝闈㈣鐩栦綆浜庨槇鍊硷紙榛樿 `0.2`锛夋椂锛?*涓嶈皟鐢ㄦā鍨?*锛?   鐩存帴濡傚疄璇存槑鈥滅幇鏈夌煡璇嗗簱涓病鏈夎兘鐩存帴鍥炵瓟鐨勫唴瀹光€濓紝骞剁粰鍑虹浉杩戞枃浠朵笌涓嬩竴姝ュ缓璁€?   閬垮厤鐢ㄢ€滆瘝鍍忎絾鏃犲叧鈥濈殑鐗囨缂栧嚭涓€涓湅浼煎悎鐞嗙殑绛旀銆?2. **鍚岄棶棰樼紦瀛橈紙鐪佹垚鏈級**锛氬巻鍙查棶杩囦笖**绛旀鏈夋晥**鐨勯棶棰橈紝鐩存帴澶嶇敤涓婃绛旀锛屼笉鍐嶈皟妯″瀷锛?   **璐熺粨鏋滐紙娌℃壘鍒?璇佹嵁涓嶈冻/闄嶇骇锛変笉缂撳瓨**锛屼笉浼氳閿欒鍥哄寲銆?3. **璇勪及鍖哄垎缂鸿瘉鎹拰璇嫤**锛氶棬妲涗笌璇勪及鍏变韩鍖归厤绠楁硶锛屾棫 trace 閲嶈瘎浼氭鏌ユ槸鍚﹀凡鏈夌浉鍏崇墖娈靛嵈浠嶈鎷掔瓟锛涙嫆绛旀ā鏉夸笉璁″叆浜嬪疄鎬у洖绛旂殑鏍囪瘑绗﹀繝瀹炲害銆傝鐩栦笉瓒虫垨璇嫤鏃跺垎鏁颁笂闄愪负 55锛岃鎷︽爣璁颁负 poor銆傝瘝闈㈠懡涓粎鐢ㄤ簬鎺掗櫎鏄庢樉鏃犲叧鍐呭锛屼笉琛ㄧず璇佹嵁瀹屾暣銆?4. **鎺у埗鍙嶉瀛︿範**锛氶棬妲涖€佹彁绀鸿瘝鍜屾墿搴撳缓璁笉浼氳嚜鍔ㄧ敓鎴愬姞鏉冿紱涓嶄粠璇勫鐞嗙敱涓涔犳潈閲嶏紝涓嶉噸澶嶆帹鑽愬凡鍙洖鏂囦欢锛屼篃涓嶅洜鎺ㄨ崘鏌愪釜鏂囦欢灏变负鍏剁埗鐩綍鍔犳潈銆?
> 閰嶇疆锛歚knowledge.retrieve.min_coverage`锛堥粯璁?0.2锛岃秺澶ц秺淇濆畧锛夈€?
---

## 2. 閰嶇疆锛堜竴涓洰褰?+ 涓€涓ぇ妯″瀷锛?
`config/config.yaml`锛?
```yaml
app:
  host: 0.0.0.0
  port: 8000
  data_dir: ./data

auth:
  bootstrap_admin:
    username: ${CODELIB_ADMIN_USERNAME:-}
    password: ${CODELIB_ADMIN_PASSWORD:-}

workspace:                       # 鈫?鍙€夌殑鍒濆鐭ヨ瘑搴擄紙鍙锛夛紱鍚庣画鍙敱绠＄悊鍛樻坊鍔犳洿澶氬簱
  path: ./sample_workspace       #   鏀规垚浣犵殑鐩綍锛屽 D:/tdx/zq/tx_msg_svr
  name: 鎴戠殑宸ョ▼
  auto_reindex_on_query: true    # 鎻愰棶鏃惰嚜鍔ㄦ劅鐭ュ彉鏇村苟澧為噺閲嶅缓
  watch_interval_seconds: 0      # >0 鍒欏悗鍙板畾鏃跺贰妫€锛堢锛?
llm:                             # 鈫?鍙厤涓€涓ぇ妯″瀷锛涚暀绌?api_key 鍗抽檷绾?  type: openai_compatible        # openai_compatible | azure_openai | anthropic | gemini
  base_url: https://api.deepseek.com/v1
  api_key: ${DEEPSEEK_API_KEY:-}
  model: deepseek-chat
  temperature: 0.2
  max_tokens: 900
  timeout: 60

memory:
  enabled: true

knowledge:
  retrieve:
    top_k: 40                    # 鍏ㄥ眬鍊欓€夋暟
    budget_tokens: 4000          # 鍏ㄥ伐浣滃尯鍏辩敤鐨勪笂涓嬫枃棰勭畻
  rerank:
    mode: llm
    top_n: 10
  answer_review: true            # 鍒嗘瀽绫婚棶棰樻鏌ヨ鐩栧害锛屽繀瑕佹椂淇

channels:
  wecom:  { enabled: false, corp_id: "", agent_id: "", secret: "", token: "", encoding_aes_key: "", webhook_url: "" }
  feishu: { enabled: false, app_id: "", app_secret: "", verification_token: "", encrypt_key: "", webhook_url: "" }
```

澶фā鍨?`type` 鏀寔鐨勫洓绉嶅崗璁紙鍚屼竴涓厤缃紝鍙敼 `type`/`base_url`/`model`锛夛細

| type | 閫傞厤瀵硅薄 | 闇€瑕佺殑瀛楁 |
|------|----------|-----------|
| `openai_compatible` | OpenAI銆丏eepSeek銆侀€氫箟銆並imi銆佹櫤璋便€丱llama銆乿LLM銆乷ne-api 绛?| `base_url` `api_key` `model` |
| `azure_openai` | Azure OpenAI | `base_url` `deployment` `api_version` `api_key` |
| `anthropic` | Claude | `base_url`(榛樿瀹樻柟) `api_key` `model` |
| `gemini` | Google Gemini | `api_key` `model` |

`api_key`銆乣base_url`銆乣model` 閮戒负绌?鈫?瑙嗕负鏈厤缃ぇ妯″瀷锛岃嚜鍔ㄧ敤銆屾绱㈡憳瑕併€嶄綔绛斻€傚瘑閽ュ缓璁敤鐜鍙橀噺锛堣 `.env.example`锛夈€?
> **鎺ㄧ悊鍨嬫ā鍨?*锛堝 hy3銆丟LM 绛夛級锛氬缓璁?`max_tokens` 璁?鈮?2048銆傝嫢棣栬疆鍝嶅簲 `finish_reason=length` 涓?`content` 涓虹┖锛堟帹鐞嗚繃绋嬪崰婊′簡棰濆害锛夛紝鏈嶅姟浼氳嚜鍔ㄦ斁澶у埌 4096 閲嶈瘯涓€娆★紝骞跺湪杩涚▼鍐呰浣忚棰濆害锛屽悗缁笉鍐嶉噸璇曘€?> `workspace.id` 鍙€夛紙缂虹渷鍙栫洰褰曞悕锛夛紱`/ask` 鐨?`workspace` 瀛楁鍙渷鐣ワ紙鍗曠洰褰曟椂锛夛紝浼?`id`銆乣name` 鎴栫洰褰曞悕閮借兘鍖归厤銆?
---

## 3. 蹇€熷紑濮?
```bash
python -m pip install -r requirements.txt          # 鍥藉唴鍙姞 -i https://pypi.tuna.tsinghua.edu.cn/simple

python -m app --config config/config.yaml index    # 寤虹储寮?python -m app --config config/config.yaml ask "娑堟伅缃戝叧濡備綍璺敱娑堟伅锛?   # 鍛戒护琛屾彁闂?python -m app --config config/config.yaml serve --port 8000           # 鍚姩鏈嶅姟
```

鍚姩鍚庢墦寮€ `http://127.0.0.1:8000/docs` 璋冭瘯銆?
**鎹㈡垚浣犵殑宸ョ▼**锛氭妸 `config/config.yaml` 鐨?`workspace.path` 鏀规垚浣犵殑鐩綍锛岄噸鏂?`index` 鍗冲彲銆?
---

## 4. 鎺ュ叆浼佷笟寰俊锛堣嚜寤哄簲鐢ㄥ洖璋冿級

1. 绠＄悊鍚庡彴鍒涘缓鑷缓搴旂敤锛岃褰?`CorpID / AgentId / Secret`銆?2. 銆屾帴鏀舵秷鎭€嶈缃?URL锛歚https://浣犵殑鍏綉鍦板潃/channels/wecom/callback`锛岀敓鎴?`Token`銆乣EncodingAESKey`銆?3. 鎶婅繖浜涘€煎～鍒?`channels.wecom`锛堟垨鐢?`WECOM_*` 鐜鍙橀噺锛夈€?4. 銆屼紒涓氬彲淇?IP銆嶅姞鍏ユ湇鍔″櫒鍑哄彛 IP锛涙湇鍔￠渶鍏綉鍙揪锛堜簯涓绘満鎴栧唴缃戠┛閫忥級銆?
閰嶇疆浜?`corp_id+secret+agent_id` 鏃朵紭鍏?*涓诲姩鍙戞秷鎭?*鍥炲锛涘惁鍒欒繑鍥?*鍔犲瘑琚姩鍥炲**锛圓ES-256-CBC锛岄伒寰畼鏂?`WXBizMsgCrypt`锛夈€?
### 鍦ㄤ紒涓氬井淇￠噷銆岄€夌煡璇嗗簱鍐嶆彁闂€?
浼佷笟寰俊娌℃湁涓嬫媺妗嗭紝鐢?*鏂囨湰浜や簰**锛堥涔﹀悓鐞嗭級锛?
1. 鐢ㄦ埛绗竴娆″彂娑堟伅锛堟垨鍙戙€屽府鍔┿€?銆岄€夊簱銆嶏級鈫?鏈哄櫒浜哄洖澶嶅簱鍒楄〃锛?   ```
   璇烽€夋嫨瑕佹彁闂殑鐭ヨ瘑搴擄紙鍥炲搴忓彿锛夛細
   1. 娑堟伅缃戝叧
   2. 鏌愪釜涓氬姟搴?   ```
2. 鐢ㄦ埛鍥炲搴忓彿锛堝 `2`锛夋垨搴撳悕 鈫?缁戝畾璇ョ敤鎴峰綋鍓嶅簱锛屽洖澶嶃€屽凡鍒囨崲鍒扮煡璇嗗簱锛歺xx銆嶃€?3. 涔嬪悗鐩存帴鍙戦棶棰?鈫?灏卞湪璇ュ簱閲屼綔绛旓紝**璺ㄦ秷鎭繚鎸?*銆?4. 鎯虫崲搴擄紝鍐嶅彂銆岄€夊簱銆嶆垨鐩存帴鍥炲簭鍙峰嵆鍙€?
瑕佺偣锛?- 缁戝畾鎸?**娓犻亾 + 鐢ㄦ埛** 鎸佷箙鍖栵紙`im_binding` 琛級锛屼簰涓嶅奖鍝嶏紱
- 浼氳瘽鎸夈€岀敤鎴?+ 搴撱€嶉殧绂伙紝鍒囨崲搴撲笉浼氫覆涓婁笅鏂囷紱
- 鍙湁涓€涓簱鏃?*鑷姩閫変腑**锛屾棤闇€閫夋嫨銆?
## 5. 鎺ュ叆椋炰功

1. 寮€鏀惧钩鍙板垱寤轰紒涓氳嚜寤哄簲鐢紝寮€鍚€屾満鍣ㄤ汉銆嶃€?2. 銆屼簨浠惰闃呫€峌RL锛歚https://浣犵殑鍏綉鍦板潃/channels/feishu/event`锛岃闃?`im.message.receive_v1`銆?3. 濉?`app_id / app_secret`锛堝寮€鍚姞瀵嗗啀濉?`encrypt_key / verification_token`锛夈€?4. 鐢宠鏉冮檺 `im:message`銆乣im:message:send_as_bot`锛屽彂甯冪増鏈€?
鏈嶅姟瀵?`url_verification` 鐩存帴杩斿洖 challenge锛涙敹鍒版秷鎭厛绔嬪嵆鍥炴墽 `{"code":0}`锛屽啀寮傛鍥炲銆?
---

## 6. 瀵硅瘽鍛戒护锛堝湪寰俊/椋炰功/鏈湴閮藉彲鐢級

| 鍛戒护 | 浣滅敤 |
|------|------|
| `/help` | 甯姪 |
| `/status` | 绱㈠紩涓庤蹇嗙姸鎬併€佸緟澶勭悊鍙樻洿 |
| `/index [full]` | 澧為噺/鍏ㄩ噺閲嶅缓绱㈠紩 |
| `/changes` | 鏌ョ湅鐩綍鍙樻洿 |
| `/remember <鏂囨湰>` | 鍐欏叆涓€鏉￠暱鏈熻蹇?|
| `/facts` | 鍒楀嚭宸茶蹇嗕簨瀹?|

鐩存帴鍙戦棶棰樺嵆闂瓟锛涗互 `!deep` 寮€澶村彲鍔犳繁妫€绱€?
CLI 涔熸彁渚涘悓绛夎兘鍔涳細`index / ask / changes / status / remember / llm`锛堣瑙?`python -m app --help`锛夈€?
---

## 7. 鐪?token 鐨勬満鍒讹紙鍚疄娴嬶級

1. **绱㈠紩涓嶅瓨鍏ㄦ枃**锛氬彧鎶藉嚱鏁?绫荤鍚嶃€佹枃妗ｅ瓧绗︿覆銆丮arkdown 鏍囬銆佸叧閿敞閲婏紝璁板綍 `file:line` 鎸囧洖婧愭枃浠躲€?2. **鎸夐绠楁绱?*锛欱M25 + 涓嫳鍚屼箟璇嶆帓搴忥紝鍙妸鏈€鐩稿叧鐨勫皯閲忕墖娈垫敞鍏ヤ笂涓嬫枃锛涚粺涓€鐢?`knowledge.retrieve.budget_tokens` 鎺у埗涓婇檺锛屼笉鍐嶆寜宸ヤ綔鍖哄垎鍒缃紱浠呭鍓嶅嚑鏉″洖婧愯绮剧‘琛屻€?3. **绛旀澶嶇敤**锛氫粎瀵硅冻澶熷畬鏁淬€佸甫鏉ユ簮涓斿綋鍓嶆绱粛鍛戒腑鍘熸潵婧愮殑绠€鍗曢棶棰樺鐢ㄧ瓟妗堬紱澶嶆潅闂鍜岄檷绾ф憳瑕佷細閲嶆柊鍥炵瓟銆?4. **澧為噺绱㈠紩**锛氬彧澶勭悊鍙樻洿鏂囦欢锛坄mtime+size` 蹇€熻烦杩囷級銆?
鐪熷疄宸ョ▼ `D:\tdx\zq\tx_msg_svr`锛?40 涓?Go 鏂囦欢锛夊疄娴嬶細

| 鎸囨爣 | 鏁板€?|
|------|------|
| 鍏ㄩ噺浠ｇ爜 token | 202,108 |
| 绱㈠紩鍧?token | 29,263锛堢害鍏ㄩ噺 **14.5%**锛岀储寮曠渷 **85.5%**锛?|
| 鍗曟闂瓟涓婁笅鏂?| ~976 token锛堢浉瀵瑰叏閲忕渷 **绾?99.5%**锛?|

> token 涓哄唴缃及绠楋紝鐢ㄤ簬棰勭畻鎺у埗涓庨噺绾у弬鑰冦€備唬鐮佸簱瓒婂ぇ锛岃妭鐪佽秺鏄庢樉銆?
## 8. 鍙樻洿鎰熺煡

- `mtime+size` 蹇€熸瘮瀵?鈫?鐤戜技鍙樻洿鍐?`sha256` 纭锛岄伩鍏嶈鎶ワ紱
- 涓夌瑙﹀彂锛氭彁闂椂鑷姩锛坄auto_reindex_on_query`锛夈€佸悗鍙板贰妫€锛坄watch_interval_seconds`锛夈€佹墜鍔?`/index` `/changes`锛?- 鍙樻洿璁板綍鍐欏叆 `change_events`銆?
## 9. 璁板繂

- 姣忔闂瓟鑷姩瀛?`qa_memory`锛堥棶棰?绛旀/寮曠敤锛夛紱
- `/remember` 瀛?`facts`锛堢敤鎴风‘璁ょ殑浜嬪疄锛夛紱
- 鍙洖鏃跺彧娉ㄥ叆鏈€鐩稿叧鐨勫皯閲忚蹇嗭紝骞舵爣娉ㄣ€屼笌褰撳墠浠ｇ爜鍐茬獊鏃朵互浠ｇ爜涓哄噯銆嶃€?
---

## 10. 鐩綍缁撴瀯

```
codelib/
鈹溾攢鈹€ app/
鈹?  鈹溾攢鈹€ main.py          # FastAPI锛堟瀬绠€鎺ュ彛锛?鈹?  鈹溾攢鈹€ bot.py           # 闂瓟缂栨帓 + 鍛戒护
鈹?  鈹溾攢鈹€ indexer.py       # 澧為噺绱㈠紩 / 鍙樻洿姣斿 / 澶氳瑷€鎶藉彇
鈹?  鈹溾攢鈹€ retrieval.py     # BM25 + 鍚屼箟璇?+ 棰勭畻鎵撳寘 + 娣辫
鈹?  鈹溾攢鈹€ memory.py        # 闀挎湡璁板繂
鈹?  鈹溾攢鈹€ store.py         # SQLite 鎸佷箙鍖?鈹?  鈹溾攢鈹€ llm.py           # 鍗曚釜澶фā鍨嬪鎴风锛? 绉嶅崗璁級
鈹?  鈹溾攢鈹€ config.py        # 閰嶇疆鍔犺浇
鈹?  鈹溾攢鈹€ watcher.py       # 鍚庡彴宸℃
鈹?  鈹溾攢鈹€ cli.py           # 鍛戒护琛?鈹?  鈹溾攢鈹€ knowledge/       # 浠ｇ爜鐭ヨ瘑搴撹兌姘村眰锛坰chema/build/query锛?鈹?  鈹溾攢鈹€ web/index.html   # 缃戦〉瀵硅瘽鐣岄潰
鈹?  鈹斺攢鈹€ channels/        # wecom.py / feishu.py
鈹溾攢鈹€ config/config.yaml
鈹溾攢鈹€ sample_workspace/    # 婕旂ず宸ョ▼锛堝彲鍒狅級
鈹溾攢鈹€ tests/               # smoke / api / web / llm / llm_live
鈹溾攢鈹€ scripts/eval_real.py # 鐪熷疄鐩綍鍙璇勪及
鈹斺攢鈹€ requirements.txt
```

## 11. 娴嬭瘯

```bash
python tests/smoke_test.py     # 绱㈠紩鈫掗棶绛斺啋璁板繂鈫掑彉鏇粹啋澧為噺
python tests/api_test.py       # 鍗曟闂瓟 + 浼佷笟寰俊/椋炰功鍥炶皟
python tests/web_test.py       # 缃戦〉浼氳瘽锛氭柊寤?杩炵画鎻愰棶/鍘嗗彶/鍒犻櫎
python tests/workbench_test.py # 宸ヤ綔鍙帮細澶氬簱闅旂 + 鍙瀹堝崼锛堝啓璺緞 403锛?python tests/auth_test.py      # 璐﹀彿瑙掕壊銆佺煡璇嗗簱鎺堟潈銆佷細璇濋殧绂汇€佹棫鏁版嵁搴撹縼绉?python tests/trace_eval_test.py # 鍏ㄩ摼璺暀鐥?+ 瑙勫垯/妯″瀷璇勪及锛堝亣 LLM锛塸ython tests/feedback_test.py  # 璇勪及闂幆锛氱粨璁衡啋boost 瑙勫垯鈫掓绱㈠姞鏉冨懡涓?
python tests/im_test.py        # 企业微信/飞书：选库/切库/用户隔离
python tests/llm_test.py       # 澶фā鍨嬮厤缃笌璇锋眰鏋勯€狅紙涓嶈仈缃戯級
python tests/llm_live_test.py  # 鏈湴鍋囧ぇ妯″瀷绔埌绔?+ 澶辫触闄嶇骇
python tests/core_quality_test.py # 闀垮垏鐗?/ 鏂囦欢鎽樿 / 瀛愰棶棰樿鐩?/ 澶氳烦涓庢柇瑷€鎺ュ湴
python tests/admin_workspace_test.py # 鐭ヨ瘑搴撶敓鍛藉懆鏈?/ 瀵硅瘽绠＄悊 / 閲嶅惎鎸佷箙鍖?/ 瓒婃潈鎷︽埅
python tests/presentation_test.py # 榛樿绠€娲佸洖绛?/ 寮曠敤鍋忓ソ / 绂佹浠ｇ爜缁嗚妭 / 澶氳疆鍋忓ソ
```

浠ヤ笂鍧囧凡閫氳繃銆?
## 12. 瀹夊叏 / 闄愬埗

- 绱㈠紩鍙鐩爣鐩綍锛屼笉鏀规簮鐮侊紱榛樿鎺掗櫎 `.git/node_modules/dist/target/logs` 涓庝簩杩涘埗澶ф枃浠讹紱
- 绠＄悊鍛橀€氳繃鐜鍙橀噺寮曞鍒涘缓锛涘瘑鐮佷互 PBKDF2 鍝堝笇淇濆瓨锛屾櫘閫氳处鍙锋寜鐭ヨ瘑搴撴巿鏉冿紱浼佷笟寰俊/椋炰功鍧囧仛绛惧悕涓庡彲閫夎В瀵嗭紱
- 妫€绱负璇嶆硶涓轰富锛堝唴缃腑鑻卞悓涔夎瘝锛夛紝璺ㄨ瑷€姒傚康妫€绱㈠彲鍚庣画鏇挎崲涓哄悜閲忔绱紱
- 鍗曟満 SQLite锛岄€傚悎鍗曞疄渚?涓皬鍥㈤槦锛涗紒涓氬井淇?椋炰功闇€鍏綉鍥炶皟鍦板潃銆?