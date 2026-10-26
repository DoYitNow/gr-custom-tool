/* Offline-built ARM. All provenance is capture metadata, never live firmware.
 * The bounded cache is RAM-only and keys the actual native parsed object.
 * Native callbacks execute outside the short cache lock. */
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;
typedef struct { u32 style, raw, property, code, legacy; } Identity;
static const Identity identities[] = IDENTITIES;
#define COUNT (sizeof(identities)/sizeof(identities[0]))
typedef struct { void *key; void *destination; u32 version, code, source, kind; } Entry;
typedef struct { volatile u32 lock; u32 cursor; Entry entries[16]; } State;
extern u32 native_raw(void *,u32), native_inverse(void *,u32), native_inverse2(void *,u32);
extern u32 native_writer(void *,void *,void *),native_style(void *,void *,void *),native_property(void *,void *,void *);
extern u32 native_version(void *,void *,void *),native_software(void *,void *,void *),native_property_ctor(void *);
extern u32 native_dispatch(void *,void *,u32),native_dispatch_raw(void *,void *,u32),native_dispatch_raw2(void *,void *,u32);
#if HAS_PRIOR_INITIALIZER
extern void prior_namespace120_initializer(void *);
#endif
static State *state(void) { void *owner=*(void *volatile *)OWNER_CELL;return owner?(State *)((u8 *)owner+OWNER_OFFSET):0; }
static void lock(State *s) { while(__atomic_exchange_n(&s->lock,1,__ATOMIC_ACQUIRE)) {} }
static void unlock(State *s) { __atomic_store_n(&s->lock,0,__ATOMIC_RELEASE); }
static Entry *find(State *s,void *key,void *dest,u32 kind) {
    u32 i;Entry *e=0;
    for(i=0;i<16;i++) { if(s->entries[i].key==key&&s->entries[i].destination==dest&&s->entries[i].kind==kind)return s->entries+i;if(!s->entries[i].key)e=s->entries+i; }
    if(!e)e=s->entries+(s->cursor++&15);
    e->key=key;e->destination=dest;e->version=0;e->code=0;e->source=0;e->kind=kind;return e;
}
static void forget(void *key,u32 by_destination) {
    State *s=state();u32 i;if(!s)return;lock(s);
    for(i=0;i<16;i++)if((by_destination?s->entries[i].destination:s->entries[i].key)==key)s->entries[i].key=0;
    unlock(s);
}
void namespace120_initialize(void *owner) {
    u32 *p=(u32 *)((u8 *)owner+OWNER_OFFSET),i;
#if HAS_PRIOR_INITIALIZER
    prior_namespace120_initializer(owner);
#endif
    for(i=0;i<sizeof(State)/4;i++)p[i]=0;
}
u32 namespace120_property_ctor(void *p) { u32 r=native_property_ctor(p);forget(p,1);return r; }
void *namespace120_property_dtor(void *p) { forget(p,1);return p; }
void *namespace120_common_context_ctor(void *p) { *(u32 *)p=0x53FDA9E8;*((u8 *)p+4)=255;forget(p,0);return p; }
void *namespace120_common_context_dtor(void *p) { forget(p,0);return p; }
static u32 version(void *entry) {
    u8 *e=(u8 *)entry,*p;if(*(u32 *)(e+8)!=4)return 0;
    if(*(u16 *)(e+6)!=1&&*(u16 *)(e+6)!=7)return 0;
    p=*(u8 **)(e+16);if(!p)p=e+12;
    return ~((u32)p[0]<<24|(u32)p[1]<<16|(u32)p[2]<<8|p[3]);
}
static u32 software_version(void *entry) {
    static const char prefix[]="RICOH GR IV Ver. ";u32 i;u8 *e=entry,*p;
    if(*(u16 *)(e+6)!=2||*(u32 *)(e+8)<22||*(u32 *)(e+8)>64)return 0;
    p=*(u8 **)(e+16);if(!p)p=e+12;
    for(i=0;i<sizeof(prefix)-1;i++)if(p[i]!=(u8)prefix[i])return 0;
    if(p[i]<'0'||p[i]>'9'||p[i+1]!='.'||p[i+2]<'0'||p[i+2]>'9'||p[i+3]<'0'||p[i+3]>'9'||p[i+4]!=0)return 0;
    return ((u32)(p[i]-'0')<<24)|((u32)((p[i+2]-'0')*10+p[i+3]-'0')<<16);
}
/* -1 delegates to the complete native decoder, 0/11 is native unknown. */
static int classify(u32 code,u32 capture,u32 property) {
    u32 i;
    if(code==0x8106||code==0x8107) {
        if((capture>>16)==0x010B) {
            for(i=0;i<COUNT;i++)if(identities[i].legacy==code)return property?identities[i].property:identities[i].style;
        }
        if((capture>>16)==0x0114)return property?(code==0x8106?21:22):(code==0x8106?32:31);
        return property?0:11;
    }
    for(i=0;i<COUNT;i++)if(code==identities[i].code||code==identities[i].legacy)return property?identities[i].property:identities[i].style;
    return -1;
}
static void apply(void *destination,u32 kind,u32 code,u32 capture) {
    int result=classify(code,capture,kind==2);if(result<0)return;
    if(kind==1)*((u8 *)destination+0x79C)=(u8)result;
    else *(u32 *)((u8 *)destination+0x94)=(u32)result; /* native property ID0x25 */
}
static void captured(void *key,void *dest,u32 kind,u32 capture,u32 source) {
    State *s=state();Entry *e;u32 code=0,v=0;if(!s)return;
    lock(s);e=find(s,key,dest,kind);
    if(source>=e->source){e->version=capture;e->source=source;}code=e->code;v=e->version;unlock(s);
    if(code)apply(dest,kind,code,v);
}
static void decoded(void *key,void *dest,u32 kind,u32 code) {
    State *s=state();Entry *e;u32 capture=0;
    if(s){lock(s);e=find(s,key,dest,kind);e->code=code;capture=e->version;unlock(s);}
    apply(dest,kind,code,capture);
}
u32 namespace120_style(void *context,void *entry,void *common) {
    u32 r=native_style(context,entry,common);
    decoded(context,common,1,((u32 (*)(void *,u32))0x5380DF54)(entry,0));return r;
}
u32 namespace120_property(void *context,void *property,void *entry) {
    u32 r=native_property(context,property,entry);
    decoded(property,property,2,((u32 (*)(void *,u32))0x5380DF54)(entry,0));return r;
}
u32 namespace120_version(void *context,void *entry,void *common) {
    u32 r=native_version(context,entry,common),v=version(entry);
    if(v)captured(context,common,1,v,2);return r;
}
u32 namespace120_software(void *context,void *property,void *entry) {
    u32 r=native_software(context,property,entry),v=software_version(entry);
    if(v)captured(property,property,2,v,1);return r;
}
static void property_tag(void *self,void *entry,u32 dir,u32 offset) {
    u32 v;void *p;if(dir!=4||*(u16 *)((u8 *)entry+4)!=0x27)return;
    p=*(void **)((u8 *)self+offset);v=version(entry);if(p&&v)captured(p,p,2,v,2);
}
u32 namespace120_dispatch(void *s,void *e,u32 d) {u32 r=native_dispatch(s,e,d);property_tag(s,e,d,12);return r;}
u32 namespace120_dispatch_raw(void *s,void *e,u32 d) {u32 r=native_dispatch_raw(s,e,d);property_tag(s,e,d,16);return r;}
u32 namespace120_dispatch_raw2(void *s,void *e,u32 d) {u32 r=native_dispatch_raw2(s,e,d);property_tag(s,e,d,16);return r;}
u32 namespace120_writer(void *a,void *entry,void *common) {
    u32 i,style=*((u8 *)common+0x79C);
    for(i=0;i<COUNT;i++)if(style==identities[i].style)return ((u32 (*)(void *,u32,u32))0x5380DD48)(entry,0,identities[i].code);
    return native_writer(a,entry,common);
}
u32 namespace120_raw(void *model,u32 style) {u32 i;for(i=0;i<COUNT;i++)if(style==identities[i].style)return identities[i].raw;return native_raw(model,style);}
u32 namespace120_inverse(void *model,u32 raw) {u32 i;for(i=0;i<COUNT;i++)if(raw==identities[i].raw)return identities[i].style;return native_inverse(model,raw);}
u32 namespace120_inverse2(void *model,u32 raw) {u32 i;for(i=0;i<COUNT;i++)if(raw==identities[i].raw)return identities[i].style;return native_inverse2(model,raw);}
/* Native summary values 34/35 also mean Custom2/Custom3 when selector != 0.
 * Mark only a confirmed custom main row between these two adjacent UI calls;
 * the marker is never written into UserData, a RAW ordinal or photo metadata. */
u32 namespace120_photo(u8 *data) {u32 i,s=data[0x153];if(!data[0x152])for(i=0;i<COUNT;i++)if(s==identities[i].style)return 0x100u|s;return ((u32 (*)(void *))0x532A1280)(data);}
u32 namespace120_movie(u8 *data) {u32 i,s=data[0x515];if(!data[0x514])for(i=0;i<COUNT;i++)if(s==identities[i].style)return 0x100u|s;return ((u32 (*)(void *))0x532A14BC)(data);}
#ifndef ICON_IDS
#define ICON_IDS {0}
#endif
static const u32 icon_ids[]=ICON_IDS;
u32 namespace120_settings_icon(void *model,u32 style) {u32 i;if(style&0x100u)for(i=0;i<COUNT;i++)if((style&255u)==identities[i].style)return icon_ids[i];return ((u32 (*)(void *,u32))0x533A6868)(model,style);}
