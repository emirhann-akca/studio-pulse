# Yeni oyuna kurulum (Studio Pulse: analitik + reklam)

Bir Unity oyununa (Unity 6000.3; 2021.3+ yeter) analitik, ödüllü/geçiş reklamı, AB/BK onay formu ve
ATT'yi **"paketleri ekle + bir ayar dosyası + oyunda üç bağlama noktası"** ile kurmak. Acorn Mart
1.0.1'de oyuna özel yazılan üç parça (yayın derlemesine kimlik yazma, açılış, deneme analitiği) artık
paketin içinde; oyunda yalnız oyuna özgü olan kalır (ödül arayüzü, olayların tetiklendiği yerler).

| Ne | Nerede | Depoda mı |
|---|---|---|
| Unity ayarı | `Assets/Resources/studio_app.json` | evet, **anahtarsız** (gizlilik metinleri var, kimlik yok) |
| Oyunun kurulum dosyası | depo kökü `studio-app.json` | evet: LevelPlay / AdMob kimlikleri (herkese açık, uygulamanın içinde zaten var) |
| Toplayıcı adresi + yazma anahtarı | stüdyonun yerel `~/Library/Application Support/studio-pulse/sdk/<etiket>.env` (izin 600) | **hayır**, hiçbir depoya girmez |
| LevelPlay API sırları | ortam değişkeni (`LEVELPLAY_SECRET_KEY`, `LEVELPLAY_REFRESH_TOKEN`) | **hayır** |

Geliştirme, test ve simülatör derlemeleri anahtarsız ayarla çıkar: ağ çağrısı yok, onay formu yok, ATT
sorusu yok, reklam tuşu yok. Yalnız yayın derlemesi kimlikleri içeri yazar ve derleme bitince dosyayı
bayt bayt geri alır (`StudioAppRelease`, docs/ADS_UNITY.md "Release keys").

## 1. Paketler

`Packages/manifest.json` (sürümler Acorn Mart 1.0.1'de denenenler):

```json
{
  "scopedRegistries": [
    { "name": "package.openupm.com", "url": "https://package.openupm.com",
      "scopes": ["com.google.external-dependency-manager"] }
  ],
  "dependencies": {
    "com.unity.services.levelplay": "9.6.0",
    "com.google.external-dependency-manager": "1.2.190",
    "com.studio.pulse": "https://github.com/emirhann-akca/studio-pulse.git?path=/unity/com.studio.pulse#<commit>"
  }
}
```

- **Studio Pulse:** depo gizliyse ortak (Hasan) git adresini çözemez. O zaman Acorn Mart gibi paketi
  projeye göm: `cp -R studio-pulse/unity/com.studio.pulse <oyun>/Unity/<Proje>/Packages/` ve
  manifest'ten git satırını sil (Unity `Packages/` altındakini kendiliğinden alır). Hangi commit'ten
  kopyalandığını commit mesajına yaz. Her dosyanın `.meta`'sı pakette hazır.
- **LevelPlay 9.x** (Ads Mediation, Unity Registry): Editor'ü bir kez **açık** (batchmode değil) başlat;
  LevelPlay yerel SDK tanımlarını indirir (`Assets/LevelPlay/Editor/IronSourceSDKDependencies.xml` ve ağ
  bağdaştırıcıları, ör. `ISUnityAdsAdapterDependencies.xml`). Bu dosyaları commit et: batchmode'da inmezler.
- **EDM4U** (OpenUPM): Android bağımlılıklarını `Assets/Plugins/Android/mainTemplate.gradle`'a yazar.
  Batchmode'da: `-executeMethod Studio.Pulse.Editor.StudioAppRelease.ResolveAndroidDependencies`
  (çıkış kodu 0 / 1). Gradle şablonlarını commit et.
- **AB/BK onayı (Google UMP):** `Samples~/AdsQuickStart/StudioPulseUmpDependencies.xml` dosyasını
  `Assets/Plugins/StudioPulse/Editor/`'e kopyala (iOS pod `GoogleUserMessagingPlatform` 3.1.0, Android
  `com.google.android.ump:user-messaging-platform` 4.0.0).
- **Scripting Define Symbols:** iOS'a `STUDIO_PULSE_ATT` ve `STUDIO_PULSE_UMP`, Android'e
  `STUDIO_PULSE_UMP`. (`STUDIO_PULSE_IOS_NATIVE`: Pulse'un gizlilik manifesti + kurulum kaynağı eklentisi;
  isteğe bağlı, anahtarsız derlemelere de geçer.)

## 2. Ayar dosyası

### 2a. Unity ayarı (anahtarsız)

`Samples~/AdsQuickStart/studio_app.json` → `Assets/Resources/studio_app.json`. Yalnız kimlik olmayanları
doldur: `privacy.privacyPolicyUrl`, `privacy.attUsageDescription` (en zorunlu, tr önerilir; ATT sorusunun
metni), `privacy.childDirected` (çocuklara yönelik değilse `false`). Bütün anahtarlar **boş** kalır;
`pacing` 0 kalır (reklam sınırları oyunun kendi kuralında).

### 2b. Oyunun kurulum dosyası: tek komut

```bash
cd ~/studio-pulse      # main'in temiz kopyası (deploy/ ve tools/ orada)
python3 tools/new_game.py kupdunyalari --name "Küp Dünyaları" \
  --ios com.emirhan.kupdunya --android com.emirhan.kupdunya \
  --privacy-url https://grichai.com/kup-dunyalari/privacy --website https://grichai.com \
  --manifest ~/kup-dunya/studio-app.json
```

`--apply` olmadan **plan**: hiçbir şey değişmez (dosya, gizli değer, servis, reklam platformu), yalnız
yapılacaklar ve yazılacak `studio-app.json` basılır. Anahtarlar ekrana asla gelmez; yalnız kimlik özeti
(toplayıcının da sakladığı 8 haneli hash). `--apply` ile:

1. **Toplayıcı yazma anahtarı** (`deploy/cloudrun/make_keys.py`, `bootstrap.sh`'ın APPS mantığı):
   `collector.env`'e oyunun etiketiyle, bundle id'lerine **bağlı** yeni anahtar (bağlı anahtar başka
   oyun için 403 alır; mevcut acornmart / zevo anahtarları korunur) → Secret Manager `pulse-write-keys`
   yeni sürüm (değer stdin'den, komut satırına girmez) → Cloud Run `pulse-collector` o sürüme sabitlenmiş
   yeni revizyon (toplayıcı anahtarları açılışta okur) → `sdk/<etiket>.env` → `/health`. GCP projesi
   `--project` (varsayılan `grich-pulse`); her gcloud çağrısına `--project` ve
   `CLOUDSDK_CORE_PROJECT=grich-pulse` verilir (makinenin varsayılanı silinmiş proje olabilir). İmaj
   yeniden derlenmez.
2. **LevelPlay** (`tools/ads_onboard.py`): `LEVELPLAY_SECRET_KEY` ve `LEVELPLAY_REFRESH_TOKEN` ortamda
   varsa iOS / Android uygulamasını bundle id ile bulur, yoksa mağaza adresinden kurar (iOS için
   `--app-store-id` gerekir; mağazada olmayan oyunun uygulamasını panelde elle aç, komut onu bulur),
   eksik `rewarded` / `interstitial` birimlerini açar, duraklatılmışları açar; kimlikler
   `networks.levelplay`'e yazılır. Plan yalnız okur.
3. **`studio-app.json`** yazılır; dosya varsa birleştirilir (içindeki kimlikler korunur, `unity_ads`
   gibi bölümlere dokunulmaz). Komutu tekrar koşmak güvenlidir.

`--apply` bulutta değişiklik yapar (Secret Manager sürümü, Cloud Run revizyonu): stüdyo sahibinin onayıyla
koşulur. Plan çıktısı (20 Kas 2026, Küp Dünyaları, bu makine): yeni `kupdunyalari` anahtarı "new",
acornmart ve zevo "kept", LevelPlay sırları ortamda olmadığı için adım atlandı, dosya yazılmadı.

**Elle kalan:** AdMob uygulama kimlikleri (`ca-app-pub-...~...`, AdMob > Apps > App settings) →
`networks.ump.ios_app_id` / `android_app_id`; AdMob > Privacy & messaging'de GDPR mesajı **yayında**.
Unity Ads'i LevelPlay'de teklifçi yapmak (Mediation > Setup > SDK Networks; `ads_onboard.py` eksikleri
`networks.unity_ads`'teki oyun kimlikleriyle liste olarak basar). `app-ads.txt` (5. adım).

### 2c. Derleme ne zaman kimlik alır

| `studio-app.json` | Davranış |
|---|---|
| `"release_inject": "store"` (varsayılan) | Development olmayan **her** derleme kimlikleri alır (BuildPipeline.BuildPlayer, Build penceresi, CI). |
| `"release_inject": "manual"` | Yalnız betiği `StudioAppRelease.BuildPlayer(opt)` (ya da `Apply` / `Restore`) çağıran derleme. Test / simülatör / cihaz derlemelerin Development değilse bunu seç (Acorn Mart böyle). |
| `"pulse_label"` | toplayıcı anahtarının etiketi; boşsa ad: "Küp Dünyaları" → `kupdunyalari` |
| `"pulse_env"` | başka bir `.env` (dosyaya göre göreli, `~` olur) |

Ortam değişkenleri: `STUDIO_APP_MANIFEST` (dosya yolu; yoksa Unity proje klasörü ve üstündeki üç klasöre,
depo kökünden yukarı çıkmadan bakılır), `STUDIO_PULSE_SDK_ENV` (`.env` yolu; başka makinede derlerken
dosyayı oraya kopyala ya da bununla göster), `STUDIO_APP_RELEASE=off` (otomatik adım kapalı).

Analitik derlemesi (reklamsız, simülatörde veri akışını görmek için):
`StudioAppRelease.BuildPlayer(opt, ads: false)` + `BuildOptions.Development`.

**Kontrol:** yayın derlemesinin günlüğünde iki satır:
`[Studio.App] release ids written ... ios=levelplay android=levelplay ump=ios android analytics=on (pulse-collector-....run.app)`
(anahtar asla yazılmaz, yalnız sunucu adı) ve `[Studio.Ads] ios build: analytics ON, ads levelplay`.
Derlemeden sonra `git status` temiz olmalı. Derleme yarıda çökse bile dosya geri gelir: kopyası
`Library/StudioPulse/`'ta bekler, Editor kapanırken ya da yeniden açılınca geri yazılır.

## 3. Ödül yerleri (oyun kodu)

Üç bağlama noktası; anahtarsız derlemede üçü de hiçbir şey yapmaz.

**a) Açılış, tek satır** (ilk sahnenin `Start`'ının ilk satırı: uygulama o an etkin, iOS ATT sorusu
ancak o zaman çıkabilir):

```csharp
StudioApp.Launch(
    started: () => OyunAnalitik.Bagla(),                         // her zaman; analitik açıksa Pulse.Core dolu
    adsReady: () => Reklam.SaglayiciAta(new StudioReklam()));    // yalnız reklam sağlayıcısı bağlandıysa
```

Bir kez koşar, hiç istisna fırlatmaz. `adsReady` çağrılmazsa oyun reklam tuşu göstermez.

**b) Ödül arayüzü oyunda kalır** (oyunun kendi reklam soyutlaması → `StudioAds`):

```csharp
sealed class StudioReklam : Reklam.ISaglayici
{
    public bool OdulluHazir => StudioAds.RewardedReady;
    public bool GecisHazir => StudioAds.InterstitialReady;
    public void OdulluGoster(Action<bool> sonuc) => StudioAds.ShowRewarded(Yer(Reklam.SonYer), ok => sonuc?.Invoke(ok));
    public void GecisGoster(Action bitti) => StudioAds.ShowInterstitial("level_end", bitti);
}
```

Raporlardaki yer (placement) adları kısa ve sabit olsun (Acorn: `double_reward`, `continue`,
`shop_coins`, `extra_life`, `level_end`). Ödül yalnız `ShowRewarded`'ın `true` sonucunda verilir.
Günlük hak, geçiş reklamı aralığı, ödeme sonrası muafiyet gibi sınırlar oyunun kuralıdır.

**c) Analitik kancaları** (`Pulse.LevelStart` / `LevelComplete` / `LevelFail` / `LevelQuit`, ekonomi,
ekran, ayar; docs/SDK_UNITY.md) ve deneme bittiğinde:

```csharp
Pulse.LevelAttempt(new PulseAttempt {
    Level = bolum, Table = tabloNo, Band = bant, Try = deneme, Won = kazandi,
    Reason = NedenKodu(neden, kazandi),          // oyunun metni → kısa kod: oyunda kalır
    Continues = devam, Boosters = joker, Seconds = saniye, Progress = ilerleme,
    Taps = dokunuslar, TapTimes = zamanlar });   // joker / devam dokunuşu -1
```

Olay `level_attempt`; dokunuş izi kurulumların %25'inde (kurulum başına bir kez seçilir,
`PulseAttemptTrace.Rate`), 300 dokunuşa kadar, olay başına en çok ~1,3 KB.

## 4. Mağaza beyanları

Anahtarsız 1.0.0 gibi bir sürüm "Data Not Collected" kalabilir; reklam ya da analitik açılınca beyanlar
değişir. Tablolar Acorn Mart 1.0.1 için doldurulanlar (LevelPlay + Unity Ads teklifçi + Studio Pulse;
kaynak: `~/cart-hero/Unity/CartHero/docs/YAYIN-1.0.1.md`). Başka ağ aracılık ediliyorsa o ağın kendi
tablosuna da bak.

### App Store Connect — App Privacy (reklam açıkken)

LevelPlay'in Apple anketi (docs.unity.com → LevelPlay → Apple privacy questionnaire) yalnız ironSource
SDK'sını kapsar; Unity Ads ve diğer ağlar kendi tablolarından eklenir.

| Veri | Toplanıyor | İzleme (tracking) için | Amaç |
|---|---|---|---|
| Identifiers → Device ID (IDFA, ATT izniyle) | Evet | **Evet** | Third-Party Advertising |
| Usage Data → Advertising Data | Evet | Hayır | Third-Party Advertising |
| Usage Data → Product Interaction (aracı ağlar paylaşırsa) | Evet | Hayır | Third-Party Advertising |
| Diagnostics → Performance Data | Evet | Hayır | Third-Party Advertising |
| Location → Coarse Location | LevelPlay: Hayır; Unity Ads ve analitik açıksa Evet | Hayır | Analytics / Advertising |

Analitik (Studio Pulse) açılınca ayrıca, **izleme için değil**, "linked": Product Interaction, Crash Data,
Performance Data, Other Diagnostic Data, Device ID (rastgele kurulum kimliği), Coarse Location (IP'den
ülke); amaç Analytics + App Functionality (docs/CONTRACT.md "Privacy mapping"). Gerçek parayla satış
varsa Purchases → Purchase History de eklenir.

App Review notu: "The app requests App Tracking Transparency permission (iOS 14.5+) before ads load;
ads are optional rewarded videos and interstitials after level wins."

### Google Play — Data safety (reklam açıkken)

LevelPlay'in Google anketi (docs.unity.com → LevelPlay → Google Data Safety Questionnaire): veri
**toplanıyor ve paylaşılıyor**, aktarımda şifreli, silme talebi yolu var.

| Kategori | Toplanan | Paylaşılan | Amaç |
|---|---|---|---|
| Location → Approximate location | Evet | Evet | Analytics; Fraud prevention, security and compliance; Advertising or marketing |
| App activity → Other actions (reklamla etkileşim) | Evet | Evet | aynı |
| App info and performance → Diagnostics | Evet | Hayır | aynı |
| Device or other IDs (reklam kimliği) | Evet | Evet | aynı |

Analitik açılınca: App activity → App interactions, App info and performance → Crash logs +
Diagnostics, Device or other IDs (kurulum kimliği); "Analytics", paylaşılmıyor. Play Console → App
content: **Advertising ID** "Evet, kullanılıyor" (AD_ID izni yalnız reklam açık derlemede eklenir),
**Ads** "Uygulamada reklam var", hedef kitle 13+ (çocuklara yönelik değilse; `privacy.childDirected`
`false`, LevelPlay çocuk kullanıcıda COPPA bayrağı ister).

## 5. Gizlilik sayfası

Adres `studio_app.json`'daki `privacy.privacyPolicyUrl` ile aynı ve App Store'daki gizlilik adresiyle aynı
alan adında olmalı (ör. `https://grichai.com/<oyun>/privacy`); `app-ads.txt` de o alanın kökünde durur.
grichai.com'da kişi adı kullanılmaz. Sayfada:

- **Reklam:** Unity LevelPlay (ironSource), Unity Ads ve aracılık edilen ağlar; reklam kimliği (iOS'ta
  IDFA yalnız ATT izniyle, Android'de AAID), IP'den kaba konum, cihaz ve reklam etkileşimi verisi.
- **Onay:** AB/BK kullanıcısına Google UMP formu; oyunda "Reklam Gizlilik Ayarları" tuşu formu yeniden
  açar (`StudioAds.PrivacyOptionsRequired` / `ShowPrivacyOptions`).
- **Analitik (açıksa):** Studio Pulse — rastgele kurulum kimliği (reklam kimliği değil), oyun içi olaylar,
  hata ve performans verisi, IP'den ülke (IP saklanmaz); verinin nerede tutulduğu ve silme talebi yolu.
- **MMP (eklenirse):** AppsFlyer.
- Üçüncü taraf politikalarına bağlantılar, iletişim adresi.

**app-ads.txt:** LevelPlay platformundaki satırları (LevelPlay/ironSource + aracılık edilen her ağ)
`https://grichai.com/app-ads.txt` olarak yayınla (UTF-8, `text/plain`, alan kökünde). Play'de mağaza
kaydı → Developer contact → web sitesi aynı alan olmalı. Biçim: `<alan>, <yayıncı kimliği>, DIRECT[, <sertifika kimliği>]`.

## Acorn Mart bu API'lerle (davranış aynı)

Acorn Mart 1.0.1 (`~/cart-hero`, mağaza incelemesinde) bu dalın paketiyle, dışarıdan görünen hiçbir
davranışı değişmeden kurulabilir. Geçiş yapılmadı (inceleme sürerken Acorn'a dokunulmuyor); aşağıdaki
kodlar ve kanıtlar bu dalda hazırlandı.

| Acorn'da bugün (oyuna özel) | Pakette | Oyunda kalan |
|---|---|---|
| `ReklamYayin.Uygula()` / `Geri()` (yayın derlemeleri), `Uygula(false)` (simülatör analitik derlemesi) | `StudioAppRelease.Apply(ads)` / `Restore()` / `BuildPlayer(opt, ads)` | ince sarmalayıcı (Türkçe günlük satırı `REKLAM_YAYIN ...`) |
| `ReklamYayin.Birlestir` (JsonUtility + LevelPlay/UMP/Pulse kopyası) | `StudioAppRelease.Merge` → `StudioAppManifest.ApplyTo` | — |
| `ReklamYayin.PulseEnvYolu` (`sdk/acornmart.env`, `STUDIO_PULSE_SDK_ENV`) | `PulseStudioPaths.SdkEnvPath` (etiket "Acorn Mart" → `acornmart`, aynı dosya, aynı değişken) | — |
| `ReklamYayin.AndroidBagimlilikCoz` | `StudioAppRelease.ResolveAndroidDependenciesSync()` | `REKLAM_BAGIMLILIK` satırı |
| `ReklamVeri.Baslat` (`StudioApp.Start` + kancalar + sağlayıcı) | `StudioApp.Launch(started, adsReady)` | `Saglayici`, `Yer()` |
| `PulseHooks.DenemeAlanlari` / `IzKodla` / `IzCoz` / `IZ_*` | `PulseAttempt.Fields` / `PulseTrace.Encode` / `Decode` / sabitler | `DenemeNedeni` (Türkçe metin → kod), `DenemeKaydi` → `PulseAttempt` eşlemesi |
| `PulseHooks.IzOrnegi` / `IzZorla` (`pulse_iz_ornek`, %25) | `PulseAttemptTrace.Sampled` / `Force` (aynı anahtar, aynı oran: güncelleyen kurulum kararını korur) | — |
| `PulseHooks.DenemeBitti` → `Pulse.Track("level_attempt", ...)` | `Pulse.LevelAttempt(attempt)` | tetikleme (`PazarMotor.DenemeBitti`) |

**`studio-app.json`'a iki satır:** `"pulse_label": "acornmart"` (zaten ad'dan çıkan etiket; açık yazmak
daha güvenli) ve **`"release_inject": "manual"`**. Neden manual: Acorn'un `IOSDerle`, `IOSDerleCihaz`,
`AndroidDerle` derlemeleri `BuildOptions.None` (Development değil); varsayılan "store" kipi bunlara da
kimlik yazardı. "manual" ile yalnız `IOSDerleYayin` / `AndroidDerleYayin` / `IOSDerleAnalitik` (bugün
`ReklamYayin`'i çağıranlar) kimlik alır, bugünkü gibi.

**Paket:** `Packages/com.studio.pulse` bu dalın `unity/com.studio.pulse`'ı ile değiştirilir (gömülü kopya).

**`ReklamYayin.cs`** (216 satır → ince sarmalayıcı; dış yüzü aynı, `SahneKur` ve `tools/reklam_dogrula.sh`
değişmez, `analitik=açık` satırı aynı):

```csharp
using System.IO;
using Studio.Pulse;
using Studio.Pulse.Editor;
using UnityEditor;
using UnityEngine;

public static class ReklamYayin
{
    public const string AYAR = StudioAppConfigFile.AssetPath;
    public const string PULSE_ENV_DEGISKENI = PulseStudioPaths.SdkEnvVariable;

    public static string ManifestYolu { get { return StudioAppRelease.FindManifest(); } }

    public static string PulseEnvYolu
    {
        get
        {
            string m = ManifestYolu;
            return StudioAppRelease.PulseEnvPath(m != null ? StudioAppRelease.ReadManifest(m) : new StudioAppManifest { name = "Acorn Mart" }, m ?? ".");
        }
    }

    public static string PulseEnvMetni()
    {
        string yol = PulseEnvYolu;
        return yol != null && File.Exists(yol) ? File.ReadAllText(yol) : null;
    }

    public static string Birlestir(string ayarJson, string bildirimJson) { return StudioAppRelease.Merge(ayarJson, bildirimJson, true, null); }

    public static string Birlestir(string ayarJson, string bildirimJson, bool reklam, string pulseEnvMetni)
    {
        return StudioAppRelease.Merge(ayarJson, bildirimJson, reklam, pulseEnvMetni);
    }

    /// <summary>Günlük satırı (tools/reklam_dogrula.sh "analitik=açık" arar): anahtar ASLA yazılmaz.</summary>
    public static string Ozet(StudioAppConfig c)
    {
        return "ios=" + (c.AdsProvider("ios") ?? "kapalı") + " android=" + (c.AdsProvider("android") ?? "kapalı")
               + " ump=" + (c.UmpConfigured("ios") ? "ios " : "") + (c.UmpConfigured("android") ? "android" : "")
               + " analitik=" + (c.PulseConfigured ? "açık (" + PulseEnvFile.Host(c.pulse.collectorUrl) + ")" : "kapalı");
    }

    public static void Uygula() { Uygula(true); }

    public static void Uygula(bool reklam)
    {
        StudioAppConfig c = StudioAppRelease.Apply(reklam);
        if (c != null) Debug.Log("REKLAM_YAYIN uygulandı: " + Ozet(c));
    }

    public static void Geri()
    {
        if (!StudioAppRelease.Applied) return;
        StudioAppRelease.Restore();
        Debug.Log("REKLAM_YAYIN geri alındı (studio_app.json anahtarsız)");
    }

    public static void AndroidBagimlilikCoz()
    {
        bool ok = StudioAppRelease.ResolveAndroidDependenciesSync();
        Debug.Log("REKLAM_BAGIMLILIK Android çözümü: " + (ok ? "TAMAM" : "BAŞARISIZ"));
        if (Application.isBatchMode) EditorApplication.Exit(ok ? 0 : 1);
    }
}
```

**`ReklamVeri.Baslat`** (`Yer()` ve `Saglayici` olduğu gibi kalır):

```csharp
public static void Baslat()
{
    StudioApp.Launch(() => PulseHooks.Bagla(), () => Reklam.SaglayiciAta(new Saglayici()));
}
```

**`PulseHooks` deneme bölümü** (~170 satır → ~25; `DenemeAnalitikTest` ve `PulseKapiTest`'in kullandığı
adlar korunur):

```csharp
public const string DENEME = PulseAttempt.EventName;
public const float IZ_ORANI = PulseAttempt.DefaultTraceRate;
public const int IZ_PARCA = PulseTrace.Parts, IZ_PARCA_DEGER = PulseTrace.PartTaps;
public const string IZ_V1 = PulseTrace.V1, IZ_KESIK = PulseTrace.Truncated, IZ_BOZUK = PulseTrace.Broken;
public static bool? IzZorla { get { return PulseAttemptTrace.Force; } set { PulseAttemptTrace.Force = value; } }
public static bool IzOrnegi { get { return PulseAttemptTrace.Sampled; } }

public static void DenemeBitti(DenemeKaydi k)
{
    if (!Acik || k == null) return;
    try { Pulse.LevelAttempt(Deneme(k)); }
    catch (Exception e) { Uyar(e); }
}

/// <summary>Oyunun deneme kaydı → SDK'nın deneme olayı (eşleme oyunda kalır).</summary>
public static PulseAttempt Deneme(DenemeKaydi k)
{
    return new PulseAttempt
    {
        Level = k.bolum, Table = k.tabloNo, Band = k.bant ?? "unknown", Try = k.deneme, Won = k.kazandi,
        Reason = DenemeNedeni(k.neden, k.kazandi), Continues = k.devam, Boosters = k.Takviye,
        Seconds = k.saniye, Progress = k.ilerleme, Taps = k.dokunus, TapTimes = k.zamanlar,
    };
}

public static List<KeyValuePair<string, object>> DenemeAlanlari(DenemeKaydi k, bool iz) { return Deneme(k).Fields(iz); }
public static string IzKodla(int[] dokunus, float[] zamanlar, out string[] t, out string[] d) { return PulseTrace.Encode(dokunus, zamanlar, out t, out d); }
public static void IzCoz(string[] t, string[] d, out int[] dokunus, out float[] zamanlar) { PulseTrace.Decode(t, d, out dokunus, out zamanlar); }
// DenemeNedeni (Türkçe metin → out_of_time / basket_full / no_moves / rush_time_up / other) olduğu gibi kalır.
```

### Kanıtlar (20 Kas 2026)

- **Deneme olayı birebir:** Acorn'un 01321e2'deki orijinal `DenemeAlanlari` / `IzKodla` / `IzCoz` kodu ile
  yukarıdaki `Deneme(k).Fields(iz)` 20.000 rastgele deneme × izli/izsiz ve 5.000 ayrı iz üzerinde
  karşılaştırıldı (sınır durumları dahil: 300'den uzun, 4094/4095 kimlik, eksik zaman, boş dokunuş):
  **45.000 karşılaştırma, 0 fark** (alan adı, sıra, tür, değer). SDK testleri Acorn'un kendi test
  vektörlerini (tohum 7 ile 137 dokunuş, 301'de kesme, 400 dokunuşlu sınır testi) ve sabit "altın"
  kodlamaları içerir (`unity/dotnet/Studio.Pulse.Core.Tests/AttemptTests.cs`, CI'de üç işletim sisteminde).
- **Yayın derlemesi birebir:** Acorn'un **kendi** `ReklamYayinTest`'i (6 test; Acorn'un gerçek
  `studio_app.json` ve `studio-app.json` dosyalarıyla, SahneKur metnine bakan 7. test hariç) yukarıdaki
  ince sarmalayıcıyla Unity 6000.3.23f1'de koşuldu: **6/6 geçti**; günlükte
  `REKLAM_YAYIN uygulandı: ... analitik=açık (...)` satırı aynı biçimde.
- **Paket Editor'de:** `Tests/Editor` (10 EditMode testi) Unity 6000.3.23f1'de geçti; dört gerçek iOS
  derlemesinde (Xcode dışa aktarımı) 15 denetim geçti: mağaza derlemesi kimlikleri taşıyor ve
  `StudioAppBuild` onları görüyor (Info.plist'te `GADApplicationIdentifier`), Development derlemesi ve
  "manual" kipteki düz derleme anahtarsız, `BuildPlayer(opt, ads: false)` yalnız toplayıcıyı yazıyor,
  başarısız derlemede bile `studio_app.json` bayt bayt geri geliyor.
- **Koşulmayan:** Acorn'un tam test takımı ve `bash tools/reklam_dogrula.sh` (Acorn deposuna dokunulmadı).
  Geçiş yapılınca önce bunlar koşulmalı: `DOGRULAMA_TAMAM` ve EditMode testlerinin tamamı.

## Kontrol listesi

1. Paketler + LevelPlay xml'leri + EDM4U Gradle şablonları commit edildi; define'lar ProjectSettings'te.
2. `Assets/Resources/studio_app.json` anahtarsız, gizlilik metinleri dolu.
3. `tools/new_game.py ... --apply` (sahip onayıyla) → `studio-app.json` commit; `sdk/<etiket>.env` bu
   makinede; AdMob kimlikleri `networks.ump`'ta.
4. Oyunda `StudioApp.Launch`, ödül arayüzü, `Pulse.LevelAttempt` ve diğer kancalar.
5. Yayın derlemesi günlüğünde `analytics=on` ve `ads levelplay`; derleme sonrası `git status` temiz.
6. Simülatörde Development + `BuildPlayer(opt, ads: false)` ile olayların toplayıcıya geldiğini gör
   (`pulse` panosu / `/live`); gerçek cihazda ödüllü reklamı bir kez izle.
7. Mağaza beyanları, gizlilik sayfası, `app-ads.txt`.
