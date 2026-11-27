# Yöntem

Sürüm 0.1.0 · [English](METHODOLOGY.md) · [Sonuçlar](RESULTS.tr.md) · [README](README.tr.md)

Bu belge, benchmark'ın Data Mask'e ne gönderdiğini, neyi kaydettiğini, her ölçümün nasıl hesaplandığını ve sayıların neyi söyleyip neyi söyleyemeyeceğini anlatır. Asıl başvuru kaynağı koddur: aşağıdaki her kuralın yanında onu uygulayan modül yazılıdır.

## 1. Ne test ediliyor

Benchmark, barındırılan Data Mask servisini resmî Python SDK'sı üzerinden, bir müşterinin çağıracağı biçimde test eder:

```python
tagged = client.anonymize(text, persist=False)  # maskeli metin ve eşleme
restored = client.restore(tagged.state, tagged.text)
```

Beş soruya yanıt arar:

1. Özgün metin tam olarak geri geliyor mu?
2. Tanımlayıcı olduğu kanıtlanabilen değerler maskeli metinde kalıyor mu?
3. Bir kez maskelenen değer belgenin her yerinde maskeleniyor mu?
4. Servisin hiç fark etmediği kişisel veri var mı?
5. Model hukuki bağlamın ne kadarını hâlâ görüyor?

Ayrıca her çağrının ne kadar sürdüğünü ve başarılı olup olmadığını kaydeder.

## 2. Derlem

Derlem, Türkçe dava dosyalarından oluşur. Her dosyada yapay zekâ yardımıyla hazırlanmış bir dilekçe ve ona eklenen belgelerin metni bulunur: duruşma zabıtları, gerekçeli kararlar, bilirkişi raporları, iddianameler, ifade tutanakları, önceki dilekçeler, fotoğraflardan ve taranmış sayfalardan okunan metinler.

| | |
|---|---:|
| Dava dosyası | 338 |
| Belge | 1.410 |
| Dilekçe | 338 |
| Ek | 1.072 |
| Karakter | 18.932.425 |
| Medyan belge boyutu | 9,8 kB |
| En büyük belge | 375 kB |

Dilekçeler hukuk, ceza, iş, idare, icra, tüketici, noter ve vergi konularını kapsar. Ek metinleri UYAP `.udf` dosyalarından, PDF, Word ve Excel dosyalarından ve taranmış görüntülerden çıkarılmıştır. Bu yüzden gerçek metin çıkarımının satır kırılmalarını ve tanıma hatalarını taşırlar. Ayrıntılı döküm `results/full/summary.json` dosyasında, `corpus` başlığı altındadır.

200 bayttan kısa on altı dosya kapsam dışında bırakılmıştır. Bunlar bir iki satırlık parçalardır; çoğu bir fotoğraftan okunan metindir.

Bu 1.410 belgenin 1.396'sı işlendi. 332.387 karakterlik bir belge servisin 120 saniyelik işlem sınırına takıldı ve HTTP 504 ile başarısız oldu. Bunun üzerine 80 kB'tan büyük belgeler gönderilmedi: 13 belge daha, yaklaşık 1,9 milyon karakter. Bu boyutun üzerindeki bir belge daha önce işlenmişti ve sonuçlara dahildir.

Belgeler bu deponun parçası değildir. Yayımlanan sonuçlar bir belgeyi, göreli yolunun SHA-1 özetinin ilk 12 onaltılık basamağıyla anar; dolayısıyla dosya adı taşımaz (`corpus.py`).

## 3. Uygulama

Her belge aynı adımlardan geçer (`runner.py`):

1. Dosya UTF-8 olarak okunur ve tek bir `anonymize` çağrısında, düz metin olarak, bütün hâlinde gönderilir.
2. Maskeli metin ve eşleme yerel olarak kaydedilir.
3. Maskeli metin, eşlemeyle birlikte `restore` çağrısına gönderilir.
4. Geri açılan metin yerel olarak kaydedilir.

Yayımlanan koşunun ayarları:

| Ayar | Değer |
|---|---|
| SDK | `bb-mask` 0.3.1 |
| Servis | `https://app.bbmask.co` |
| `persist` | `False`: servis hiçbir şey saklamaz, eşlemeyi yanıtta döndürür |
| `placeholder_style` | `angle` (`<PERSON_0>`) |
| `keep_legal_form`, `health_data`, `llm_review` | kapalı (varsayılan) |
| Eşzamanlılık | 1: istekler tek tek gönderilir |
| İstemci zaman aşımı | 150 sn (koşunun ilk bölümünde 400 sn) |
| Ara | koşunun büyük bölümünde her belgeden sonra 3 sn |
| Gönderilen en büyük belge | 80 kB (2. bölüme bakın) |

Koşu birkaç oturumda yapıldı. Duran bir koşu aynı koşu kimliğiyle sürdürülür; başarıyla işlenen belge yeniden gönderilmez.

**Süre ölçümü.** Süre, SDK çağrısının başından sonuna geçen ve istemcide ölçülen zamandır. Ağ süresini de içerir. Koşu tek bir makineden, sıradan bir internet bağlantısı üzerinden yapılmıştır. Yani sayılar servisin içindeki işlem süresini değil, çağıran tarafın yaşadığını gösterir.

**Yeniden deneme.** Bir çağrı yalnızca HTTP 429, 502 veya 503 yanıtından sonra, yayımlanan koşuda en çok bir kez yinelenir. Zaman aşımı (504) yeniden denenmez, çünkü servis isteği tamamlamış olabilir. Son denemesi başarısız olan belge başarısız sayılır.

**`restore` girdisi.** `restore`, maskeli belgenin tamamını alır. Bir modelin yanıtında çoğu zaman yer tutucuların yalnızca bir kısmı bulunur. Bu yüzden bu en sıkı girdidir: belgedeki her yer tutucunun çözülmesi gerekir.

## 4. Ölçümler

Bütün ölçümler kaydedilen dosyalardan, çevrimdışı hesaplanır (`metrics.py`). Bir ölçümü değiştirmek servise yeni bir çağrı gerektirmez.

Maskeli metinde arama yapılmadan önce bütün yer tutucular metinden çıkarılır. Yani bir değer, bir yer tutucunun içinde “bulunamaz”.

### 4.1 Gidiş-dönüş

Geri açılan metin özgün metinle karakteri karakterine aynıysa belge geçer. Yalnızca boşluk karakterlerinde farklılaşan belgeler `summary.json` içinde ayrıca sayılır ve bu testte yine başarısız kabul edilir.

Rapor, maskeli metinde bulunup eşlemede karşılığı olmayan yer tutucuları da sayar. Böyle bir yer tutucu, geri açılan metinde ham etiket olarak kalırdı.

### 4.2 Kanıtlanabilir tanımlayıcılar

Elle yapılmış etiket olmadığından benchmark, metinden çok az kuşkuyla tanınabilen tanımlayıcıları kullanır (`detectors.py`):

| Tanımlayıcı | Ne zaman sayılır |
|---|---|
| T.C. kimlik numarası | 11 hane, ilk hane 0 değil, iki sağlama basamağı da doğru |
| IBAN | `TR` ve ardından 24 hane, bitişik ya da gruplar hâlinde, ISO 13616 mod-97 denetiminden geçiyor |
| E-posta adresi | adres sözdizimi |
| Cep telefonu | Türkiye cep telefonu biçimi: `05xx…`, `+90 5xx…`, `0 (5xx)…` |
| Plaka | il kodu, 1-3 harf, 2-4 rakam ve hemen ardından “plaka” sözcüğü |

Her denetim hem özgün hem maskeli metinde çalışır. Değerler önce ortak biçime getirilir; `TR67 0001 …` ile `TR670001…` aynı değerdir. Her değer için kalan sayı, değerin özgün metindeki ve maskeli metindeki geçiş sayılarından küçük olanıdır.

Kalan her değer bir sızıntıdır. Bunda etiketleyen kişinin yorumu yoktur; bu yüzden gizlilik tarafının başlıca göstergesi budur. Aynı zamanda dardır: adlar, adresler ve kurumlar hakkında bir şey söylemez.

Rastgele 11 haneli bir sayı, kimlik numarası denetiminden yaklaşık yüzde bir olasılıkla geçer. Dolayısıyla sayılan kimlik numaralarının küçük bir bölümü başka sayılar olabilir. Bu, kalan bir değerin anlamını değiştirmez: geçerli kimlik sağlaması olan bir sayı okunabilir bırakılmıştır.

### 4.3 Maskeleme tutarlılığı

`persist=False` ile servis maskelediği her değeri döndürür. Benchmark bu değerlerin her birinin özgün metinde ve maskeli metinde kaç kez geçtiğini sayar. Eşleşme tam sözcük olmalıdır; sözcükler arasındaki boşluk, herhangi sayıda boşluk ya da satır sonu olabilir.

- **Okunabilir kalan**, değerin tam yazıldığı biçimiyle eşleşmeleri sayar.
- **Okunabilir kalan (farklı harf büyüklüğü)**, aramayı büyük/küçük harfe bakmadan, Türkçe kurallarıyla (`I`/`ı`, `İ`/`i`) yineler. Yalnızca birden çok sözcüklü ya da rakam içeren değerlere uygulanır. “Deniz” ya da “Güven” gibi tek bir ad aynı zamanda sıradan bir sözcüktür; harf büyüklüğüne bakmadan aramak sıradan sözcükleri sızıntı sayardı.

Dört karakterden kısa değerler atlanır. `JOB_TITLE` kapsam dışıdır, çünkü değerleri (“Av.”, “Dr.”) kimseyi tanıtmaz.

Kalan bir geçiş nasıl okunmalı:

- Kişi adı ya da numara ise sızıntıdır: servis değeri biliyordu ve bir geçişini okunabilir bıraktı.
- Yer ya da kurum adı ise sızıntı da olabilir, fazla eşleşme de. Servis “Tarım” gibi genel bir sözcüğü bir kez kurum olarak maskelediyse, o sözcüğün diğer bütün kullanımları, hiçbir şey açığa çıkmadığı hâlde okunabilir kalan sayılır.

Bu ölçüm yakalama oranını değil tutarlılığı ölçer. Servisin hiç bulamadığı bir değer eşlemede yoktur ve sayılamaz.

### 4.4 Dil modeliyle ikinci denetim

Yukarıdaki iki denetimin bir kör noktası vardır. Servisin hiç yakalamadığı bir ad sağlama basamağı taşımaz ve eşlemede yoktur; iki denetim de onu göremez. Bu kör noktaya bakmak için her belgenin maskeli metni bir dil modeline verilir ve modelden hâlâ okuyabildiği kişisel verileri listelemesi istenir (`audit.py`). Maskeli metin, bir modelin gerçek kullanımda alacağı metindir; yani ürünün göndermeyi amaçlamadığı hiçbir şey gönderilmez.

Belgeler, satır sonlarından kesilen en çok 8.000 karakterlik parçalar hâlinde gönderilir. Modelden şu tür değerler istenir:

| Tür | Servisin şu türleriyle karşılaştırılır |
|---|---|
| Kişi adı (olayda yer alan biri) | `PERSON` |
| Kimlik veya pasaport numarası | `TR_NATIONAL_ID`, `TR_ID_DOCUMENT` |
| Telefon numarası | `PHONE_NUMBER` |
| E-posta adresi | `EMAIL_ADDRESS` |
| IBAN, hesap veya kart numarası | `IBAN_CODE`, `BANK_ACCOUNT`, `CREDIT_CARD`, `CARD_CVV` |
| Plaka | `TR_PLATE` |
| Açık adres | `LOCATION`, `POSTAL_CODE` |
| Doğum tarihi | `DATE_OF_BIRTH` |
| Diğer tanımlayıcı numara | `TR_TAX_ID`, `TR_REGISTRY_NO`, `TR_MERSIS`, `TR_DRIVER_LICENSE`, `TR_UETS`, `TR_ETTN` |
| Şirket veya başka özel kuruluş | `ORGANIZATION` |
| Hukuk literatürü olarak atıf yapılan yazar | ayrı listelenir, sızıntı sayılmaz |

Modele mahkemeleri ve diğer devlet kurumlarını, kanunları, dosya numaralarını, tutarları, doğum tarihi dışındaki tarihleri ve tek başına geçen şehir adlarını bildirmemesi söylenir. Dosya numaraları 4.5 numaralı bölümdeki desenle denetlenir.

Modelin yanıtı, herhangi bir şey sayılmadan önce denetlenir:

- Bir bulgu ancak metni maskeli belgede, yer tutucuların dışında, tam sözcük olarak geçiyorsa sayılır. Geçmeyen bulgular reddedilir ve rapor kaç tane olduğunu yazar.
- Örtüşen bulgular bir kez sayılır. “Ahmet Yılmaz” ile “Yılmaz” aynı sözcüğü iki kez saydırmaz.
- Her bulgu eşlemeyle karşılaştırılır. Servis aynı değeri ya da onun daha uzun veya daha kısa bir biçimini belgenin bir yerinde maskelediyse bulgu **başka yerde maskelenmiş** sayılır: bu bir tutarsızlıktır. Aksi hâlde **hiç yakalanmamış** sayılır: bu bir kaçaktır.

Bir tür için **tahmini maskeleme oranı** `maskelenen / (maskelenen + okunabilir kalan)` biçiminde hesaplanır. `maskelenen`, tablodaki servis türlerine ait yer tutucuların sayısıdır. Dosya numaraları bu sayının dışında tutulur.

Bu bir tahmindir ve iki yönde de yanılabilir:

- Model bazı değerleri gözden kaçırır. Kaç tanesini kaçırdığını ölçmek için, servisin maskelediği değerler bir belge örnekleminde bilerek açılır ve modelin bunların ne kadarını bildirdiği sayılır (`bbmask-bench audit --calibrate`). Servisin kendiliğinden bıraktığı bir sızıntı, bilerek açılandan daha zor görülebilir; yani modelin gerçek sızıntılardaki oranı muhtemelen daha düşüktür.
- Model, kişisel veri olmayan değerleri de bildirir; örneğin bir markayı ya da genel bir sözcüğü. En zayıf tür şirket adlarıdır: bir adın taraf mı yoksa geçerken anılan bir ad mı olduğu bir yorumdur.
- Servis türleri ile modelin türleri bire bir örtüşmez. Örneğin `LOCATION`, modele bildirmemesi söylenen şehir adlarını da içerir.

Bulgular kişisel veridir. `runs/` altında saklanır ve yayımlanmaz; sonuçlarda yalnızca sayılar bulunur.

### 4.5 Bağlamın korunması

Maskeleme hukuki akıl yürütmeyi okunabilir bırakmalıdır. Altı tür ifade, harfler küçültüldükten sonra özgün metinde ve maskeli metinde sayılır:

| İfade | Örnek |
|---|---|
| Kanun atfı | `2918 sayılı` |
| Madde atfı | `91. maddesi`, `m. 49`, `md. 17/2` |
| Yüksek mahkeme adı | `Yargıtay`, `Danıştay`, `Anayasa Mahkemesi` |
| Karar/esas numarası | `2019/54321 E.`, `2018/6543 K.` |
| Tarih | `14.06.2017` |
| Tutar | `400.000,00 TL` |

Korunan oran, hâlâ bulunan ifade sayısının özgün metindeki sayıya bölümüdür. Bir karar numarasının ya da tarihin maskelenmesi kendi başına hata değildir: esas numarası tarafları tanıtabilir, doğum tarihi kişisel veridir. Tablo, modelin artık neyi göremediğini gösterir; bunu kendi kullanım amacınıza göre değerlendirirsiniz.

Rapor ayrıca her belgenin karakterlerinin ne kadarının yer tutucularla değiştirildiğini verir.

### 4.6 Süre ve güvenilirlik

Yüzdelikler en yakın sıra yöntemiyle hesaplanır. İşlem hızı, toplam karakter sayısının `anonymize` çağrılarında geçen toplam süreye bölümüdür. Hem `anonymize` hem `restore` hatasız döndüyse çağrı başarılıdır.

## 5. Oranlar nasıl bildirilir

- Oranlar belgeler üzerinden değil, geçişler üzerinden hesaplanır. Belge sayıları yanlarında verilir.
- Yüzdeler aşağı yuvarlanır. Bir oran ancak hiçbir şey kalmadıysa %100 olarak gösterilir.
- README dosyalarındaki ve `RESULTS.tr.md` içindeki her sayı, `bbmask-bench report` tarafından `results/full/summary.json` dosyasından üretilir.

## 6. Sınırlar

- **Kesin yakalama oranı yok.** Etiket olmadan, servisin tümüyle kaçırdığı kişisel veri yalnızca dil modeli denetimiyle tahmin edilebilir. Kesin bir oran için elle etiketlenmiş belge gerekir.
- **Tek dil, tek alan.** Türkçe hukuk metni. Başka tür belgelerde sonuçlar farklı olabilir.
- **Tek derlem.** Dilekçeler yapay zekâ ile hazırlanmıştır ve elle yazılmış dilekçelerden daha düzenli olabilir. Dava türlerinin ve eklerin dağılımı tek bir derlemin dağılımıdır.
- **Yalnızca varsayılan seçenekler.** `llm_review`, `health_data`, `keep_legal_form`, dosya girdisi ve çok belgeli çağrılar ölçülmemiştir.
- **Tek koşu.** Her belge bir kez gönderilmiştir. Süre, o andaki servis yüküne ve ağa bağlıdır.
- **Üreticinin yaptığı ölçüm.** Benchmark'ı Data Mask'i geliştiren ekip yazmış ve çalıştırmıştır. Kod, belge başına sonuçlar ve bu açıklama, çalışmanın denetlenebilmesi ve yinelenebilmesi için yayımlanmıştır.

## 7. Koşuyu yinelemek

```bash
bbmask-bench run --corpus <derlem> --run-id full --retries 1 --max-bytes 80000 --pause 3
bbmask-bench audit full --model gpt-6-luna
bbmask-bench audit full --model gpt-6-luna --calibrate 40
bbmask-bench report full --publish
```

`run`, çalışma dosyalarını `runs/full/` altında tutar. Bu dizin her belgenin maskeli metnini ve eşlemesini, yani kişisel veri içerir ve git tarafından yok sayılır. `report --publish` paylaşılabilir dosyaları yazar:

| Dosya | İçerik |
|---|---|
| `results/full/summary.json` | bütün toplu sayılar |
| `results/full/documents.jsonl` | belge başına bir satır: özetlenmiş kimlikler, boyut, süreler, sayılar |
| `results/full/latency.svg`, `entities.svg` | grafikler |
| `docs/v0.1.0/RESULTS.md`, `RESULTS.tr.md` | tam rapor |

Bu dosyaların hiçbirinde belge metni, dosya adı, istek kimliği, hesap bakiyesi ya da API anahtarı yoktur.
