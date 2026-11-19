"""Exact local-station hosts from iHeartMedia's directory. See docs/PUBLISHER-BREADTH.md."""

# The compressed spelling below stores station labels, not a wildcard.
IHEART_HOSTS = frozenset(label + '.iheart.com' for label in """
1003hallelujahfm 1003thepeak 1003theriver 1005louisville 1005thefox 1005thewolf 1007wzxl
1011thebeat 1013 1013big 1013kissfm 1013wnco 1015 1015elpatron 1015theriver 1019bigwaax 101kgb
101magic 1021thebull 1023elpatron 1023wsus 1025thebear 1025thebull 1025thefox 1025wynr
1027jackfm 1027kissfm 1027thewolf 1029thelake 102jkx 102ktra 102theriver 1031austin 1031kcda
1031thebeat 1031wndh 1033thebeat 1035kissfm 1035thebeat 1037cky 1037thefox 1037theq 1037wllr
1037wqol 103jamz 103xonline 1041theedge 1041thespot 1043hallelujahfm 1043myfm 1045latinohits
1045snx 1045thebeat 1045thecat 1047kissfm 1049bobfm 1049foxsports 1049thepatriot 1051kocean
1051rocks 1051thewolf 1053thebreeze 1053thefox 1055online 1055thebeat 1057manup 1057thebull
1059thebrew 1059therock 1059thex 1061fmtalk 1061kissfm 1061thebullyoungstown 1061thetwister
1063thebear 1063thebrew 1065 1065thelake 1067thebull 1067theeagle 1067wllz 106wcod 1070nbcsports
1071kissfm 1073rocks 1075kissfm 1075thebigbuck 1075theriver 1077thefox 107mus 107nus
1090thepatriot 1150wima 1190kex 1190talkradio 1230foxsports 1230thegambler 1230wsok 1340thegame
1340wraw 1350thegambler 1360kktx 1360kmjm 1400foxsports 1400kuno 1430kasi 1440thefan 1440wgig
1450wkip 1460kltc 3wsradio 550thezone 55krc 570wkbn 590kcsj 590kqnt 600kcol 600wmtradio 600wrec
610wtvn 620kmns 640whlo 650keni 700wlw 710wor 720thevoice 790louisville 790waeb 800wvhu 921ctq
921thebeat 925kgb 925kissfm 925rocks 925thebreeze 925wpap 929thebigdog 929tomfm 933flz
933fmthewolf 933kjr 933odc 933theeagle 935thepatriot 937now 937thebeathouston 937thebull
937theriver 939beat 939litefm 939mia 939thewolf 941kodj 941thebeat 941zbq 943wsc 945rocks
945wpti 947bobfm 947theref 949thebeat 949thebull 949tnt 94hjy 94rock 950kjr 951wayv 951zzo
953beach 953bull 953thebeat 955thebull 955themountain 957bigfm 957thejet 957theparty 957wdae
95ksj 95wxtk 960weli 960werc 961kiss 961kissonline 961srs 961thefox 961thegame 961theriver
961therocket 961wtth 963kissfm 963starcountry 965kisscountry 965thebull 965thespear 967kissfm
967stevefm 969myfm 969thebullfm 969thedogrocks 969thegame 969thekat 96rock 970kfbx 971doubleq
971zht 973kissfm 973planetradio 973thebeat 973thebullicons 975online 975wcos 977rnb 977thebrew
979espn 979kissfm 97kicksfm 981kvet 981thebreeze 981thebullicons 983try 985elpatron 985thecat
985thefox 987theriver 989kkzx 989radionow 98rock 98rockme 98txt 991kcad 991kggi 991kglx 991wqik
993thebeat 993thefox 995kissfm 995thefox 995theriver 997thefox 997thelake 999xtc 99kisscountry
accion979 accionjacksonville aggie96 alice955 alt1023fm alt1033 alt1045 alt1045philly alt1047
alt1057 alt1073 alt955 alt961 alt975austin alt987fm am1300thezone am570lasports avlsports880
b100wobb b101 b104 b1047 b1065 b1073online b1075country b93 b939country b95forlife b95radio
b973fm b97fm b98fm beat1053 beat925 beatcolorado big1047 big1059 big1065 big1073 big939
big955chicago big979 big989 big993 bigdog106 bigdog959 bigi1079 bill95 bob949 buckeyecountry105
buckeyecountry943 bull1057 business991 businessnews975fm buzztulsa c101 catcountry1071
channel933 channel955 channel963 channeldayton cincinnatiproject cities971 classichits1390
cool925 country1005 country925 dc101 downtownradio977 dve eagle102 eagle1075 eagle929online
easy1350 edgelittlerock elpatron935 elpatron967 elpatronphoenix espn1530 espnradio1300 fm106
fm97 foxphlgambler foxrocks foxsports1070 foxsports1280 foxsports1360 foxsports1400
foxsports1400wheeling foxsports1420 foxsports1430 foxsports1450 foxsports1460 foxsports1550
foxsports1700 foxsports710 foxsports800 foxsports910 foxsports930 foxsports940 foxsports960
foxsports970am foxsports975 foxsports980 foxsports990 foxsportsabq foxsportsam1280 foxsportspgh
foxsportspueblo foxsportsradio1230 foxsportsradio1340 foxsportsradio1350 foxsportsradio1400
foxsportsraleigh foxsportsthegame freedom1047 freedom937 froggy999 fsr920 g105 gator1079
gatorrocks greatesthits1037 hallelujah1051 hallelujah1520am hallelujah1600 hallelujah940
hallelujah955 hallelujahfm heaven600 hits1061seattle hits961 hitscarolina hooptown1015 hot1025
hot1079 hot949fm hot983 hot995 hotabq hotelpaso hotmix1019 icons910 iheartsportsbayarea
iheartsportsdc iheartumadison inspiration1390 island985 jackseattle jamn1075 jamn933 jamn945
jamn957 jamzhawaii jetradio1400 k102 k103 k93country k97fm k99country k99fm kase101
kashcountry1075 kbco kbgo kbmr kbpi kbpisouth kc101 kcjb910 kcycountry kdon kdwb kee100 kegl
kez999 kfab kfan kfanplus kfbk kfiam640 kfmqrock1061 kfyi kfyr kg95 kgbx kgor kgot khey khey1380
khow khvhradio kiak kickerfm kiisfm kiixcountry kiiz kiss1023 kiss1027fm kiss1039 kiss1045fm
kiss1067 kiss1079 kiss108 kiss957 kiss959fm kiss981 kiss983 kissalbany kisscincinnati
kisscleveland kissfm1031 kissfm1071 kissfmcolorado kissfmhv kissfmrgv kisslima kisswheeling
kix104 kix993 kixcountry929 kj103fm kj97 kjkj kkrq klou klvi kmag991 kmcx kmel kmod knixcountry
knrs knst koacolorado kogo koltfm kool1045 kool108 kost1035 kpht955 kprcradio kprr kqdy krab
krdu1130 krq ksabfm ksskradio kssn kste ktcl ktex ktok ktom ktrh ktsmradio ktu kube933 kwhn kxic
kxno kxy kykr kz103 kzbb kzep kzok kzrx921 lamegaorlando lamezcla949 lapreciosa1057
lapreciosa929 lapreciosa987 laser1017 litefm literock993 lonestar925 love1011 magic107 magic1079
magic937 magic96 magic981 magic989fm magictucson majic1049stl majic959 mega100fm megatucson
mia921 miss103 mix1029 mix1033 mix1033fm mix1047 mix1065 mix1077 mix923fm mix931 mix931online
mix957 mix961 mix967online mix969 mix96online mix973wheeling mix977 mix981fm mix989 mix995triad
mix999fm mixgulfcoast moose106 my100fm my1025 my103q my995fm my999radio myhot105 mykiss951
mykix1009 mymagic101 mymagic97 mymagic989 mymix100 mymix945 mysouth1061 myv101 mywdia neon931
newcountry1039fm newcountry1079 newsradio1049 newsradio1170 newsradio1290wtks newsradio1410
newsradio1470 newsradio540 newsradio710 newsradio910wltp newsradio967 newsradiori newsradiowkcy
newstalk1090 newstalk1130 newstalk1230 newstalk1400online newstalkwkmq now1051 now933fm
oldies1320 oldies935 party1077 patriot1360 patriotla pop991 power1009 power1051 power1053
power107 power620 power973 power99 powertalk1360 powertalk1460 powertalk967 pyx106 q1019 q102
q1033 q1041 q1043 q1045 q105fm q106 q92hv q93 q95 radio945fm radio949 radio951 radionow1057
radiosunny real1039 real1039fm real1051 real1061cleveland real923la real931 real959 real963
real983 real993 realoldies1470 realradio retro979 rewind921 ripcityradio rock1017fm rock101fm
rock102memphis rock103columbus rock1053 rock1067 rock107mb rock921 rock949omaha rock955chi
rock955williamsport rock967 rocketerie rovrocks rumba100 rumba1061 rumba1069 rumbalancaster
rumbareading rumbatampabay salinaslapreciosa shenandoahcountryq102 sl100 softrock989 sports1340
sportsradio1330 sportsradio1390 sportsradio957 sportssd sportstalk790 star1019 star1021 star1029
star104 star1043 star941fm star971 sunny100 sunny1017 sunny102fm sunny1063online sunny1065
sunny985 sunny99 sunny999fm t102 talk1200boston talkradio1059 talkradio1080 tampabaysmix
tcrcountry tejano1600 thebear929 thebeat1037 thebeat1067 thebeatatx thebeatcincy thebeatcolumbia
thebeatflorence thebeatflorida thebeatgulfcoast thebeatnewhaven thebeatstl thebig98
thebiglegend983 thebreeze1077 thebull1017 thebull1047 thebullabq thebullcountry
thebullsacramento thebusfm thebuzz thecatfm thefanec theflo1029 thefox thekat theriver1059
theriver973 therockfms thetwister throwback1053 throwback963 throwbackmiami throwbacksavl
ticket760 tk101 tu949fm twincitiesnewstalk us1035 us969 us97 v100 v1011fm v1015 v1017 v103 v97fm
waco100 wamz washfm wave927 waxe1370 wbbq wbhpam wbig wbul wbznewsradio wccfam wccfradio wchoam
wcjmthebull wcol wcvu wdasfm wdov wdrm wdsd webn wercfm wescfm westmichiganstar wezl wflafm
wflanews wflaorlando wfmf wfxnthefox wgar wgci wgiram wgmz wgy wham1180 whas wheb whnz whoradio
whp580 whyn wiba wibafm wibb wild104 wild1063 wild949 wild955 wildcountry999 wilm wiod wiot wjbo
wjbt wjdx wjiz wjjz wjlbdetroit wjno wjrr wjyz wkdd wkdwam wkkj wkqq wlac wlap wloqradio wmanfm
wmeq wmji wmmbam wmms wmrn wmzq wnci wncoam wnic wnnj wnoe wnok woai woc1420 wolfradio933 wone
wonw1280 woodradio wovk wpoc wqmf wrak wrdu wrko wrno wrqk wrwdcountry wsfcam wspd wsrw wsrz
wsslfm wsyr wtag wtak wtam wtf977rocks wtkg wtue wvoc wwnc wyht wyldfm wynkcountry wzlx wzzr
x1015 xl1067 xl93 y100 y100florida y1025 y102reading y93 y94fm y969 z100 z100portland z100radio
z1043 z104fm z105 z1057atlanta z106 z1077 z107fm z93hv z95live z98rocks
""".split())
