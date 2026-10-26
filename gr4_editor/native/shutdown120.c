/* 1.20 shutdown override. Choice zero always executes the intact official
 * EndingScreenInfo path, including its downloaded B:\GB_DL.jpg image. */
#include <stdint.h>
typedef struct { uint32_t magic, choice; } Shutdown120State;
typedef struct { uint32_t choice, resource; } Shutdown120Choice;
static const Shutdown120Choice choices[] = SHUTDOWN120_CHOICES;
static const uint32_t text_ids[] = SHUTDOWN120_TEXT_IDS;
static const uint16_t preview_icons[] = SHUTDOWN120_PREVIEW_ICONS;
#define FN(at,type) ((type)(at))
#define MAGIC 0x32424753u

static Shutdown120State *state(void) {
    uint8_t *p = *(uint8_t *volatile *)OWNER_CELL;
    if (!p) p = FN(OWNER_GETTER,uint8_t *(*)(void))();
    return p ? (Shutdown120State *)(p + OWNER_OFFSET) : 0;
}
static uint32_t allowed(uint32_t choice) {
    for (uint32_t i=0;i<CHOICE_COUNT;++i) if (choices[i].choice==choice) return 1;
    return 0;
}
static uint32_t normalize(Shutdown120State value) {
    if (value.magic==0x31424753u) {
        if (value.choice==1) value.choice=LEGACY_CHOICE_1;
        else if (value.choice==2) value.choice=LEGACY_CHOICE_2;
        else return 0;
    } else if(value.magic!=MAGIC) return 0;
    return allowed(value.choice) ? value.choice : 0;
}
#if HAS_PRIOR_INITIALIZER
extern void prior_shutdown120_initializer(uint8_t *owner);
#endif
void shutdown120_initialize_owner(uint8_t *owner) {
#if HAS_PRIOR_INITIALIZER
    prior_shutdown120_initializer(owner);
#endif
    Shutdown120State *p=(Shutdown120State *)(owner+OWNER_OFFSET);
    p->magic=MAGIC; p->choice=0;
}
uint32_t shutdown120_get(void) {
    Shutdown120State *p=state();
    if(!p) return 0;
    p->choice=normalize(*p);p->magic=MAGIC;
    return p->choice;
}
uint32_t shutdown120_set(uint32_t choice) {
    if(!allowed(choice)) return 0;
    uint32_t previous=shutdown120_get();
    Shutdown120State *p=state();
    if(!p || previous==choice) return 0;
    p->choice=choice;return 1;
}
static void *apdata(void) {
    return *(void **)((uint8_t *)FN(APDATA_FACTORY,void *(*)(void))()+8);
}
uint32_t shutdown120_save_global(void) {
    Shutdown120State value={MAGIC,shutdown120_get()};
    return FN(APDATA_WRITE,uint32_t (*)(void *,uint32_t,uint32_t,const void *))
        (apdata(),SHUTDOWN_RECORD,8,&value);
}
void shutdown120_load_global(void) {
    Shutdown120State *p=state(); if(!p)return;
    p->magic=MAGIC;p->choice=0;
    void *ap=apdata();
    if(!FN(APDATA_EXISTS,uint32_t (*)(void *,uint32_t))(ap,SHUTDOWN_RECORD))return;
    if(FN(APDATA_LENGTH,uint32_t (*)(void *,uint32_t))(ap,SHUTDOWN_RECORD)!=8)return;
    Shutdown120State value={0,0};
    if(FN(APDATA_READ,uint32_t (*)(void *,uint32_t,uint32_t,void *))
        (ap,SHUTDOWN_RECORD,8,&value))p->choice=normalize(value);
}
extern uint32_t prior_shutdown120_save(void *mode);
extern uint32_t prior_shutdown120_load(void *mode,void *current,void *a2,void *a3);
uint32_t shutdown120_state_save(void *mode) {
    uint32_t result=prior_shutdown120_save(mode);shutdown120_save_global();return result;
}
uint32_t shutdown120_state_load(void *mode,void *current,void *a2,void *a3) {
    uint32_t result=prior_shutdown120_load(mode,current,a2,a3);shutdown120_load_global();return result;
}
__attribute__((naked)) static uint32_t official_background(void *controller) {
    __asm__ volatile("mov ip, sp\n b official_shutdown120_background_body");
}
uint32_t shutdown120_request_background(void *controller) {
    uint32_t selected=shutdown120_get();
    if(!selected)return official_background(controller);
    for(uint32_t i=1;i<CHOICE_COUNT;++i)if(choices[i].choice==selected) {
        void *manager=FN(APP_GET,void *(*)(void))();
        void *display=FN(DISPLAY_GET,void *(*)(void *))(manager);
        return FN(JPEG_REQUEST,uint32_t (*)(void *,uint32_t))(display,choices[i].resource);
    }
    return official_background(controller);
}
__attribute__((naked)) static uint32_t official_set(void *self,uint32_t choice,const void *data,uint32_t size) {
    __asm__ volatile("mov ip, sp\n b official_shutdown120_set_body");
}
/* A successful GR WORLD/native image change takes precedence over an older
 * custom override. A failed official update must leave the override intact. */
uint32_t shutdown120_official_set(void *self,uint32_t choice,const void *data,uint32_t size) {
    uint32_t result=official_set(self,choice,data,size);
    if(result && shutdown120_set(0))shutdown120_save_global();
    return result;
}

static void *list(void *controller){return *(void **)((uint8_t *)controller+0x168);}
static void *userdata(void){return FN(USERDATA_GET,void *(*)(void))();}
static uint32_t selected(void *controller){return FN(LIST_SELECTED,uint32_t (*)(void *))(list(controller));}
static uint32_t label(uint32_t row){
    if(row<2)return FN(STATISTICS_LABEL,uint32_t (*)(void *,uint32_t))(0,row);
    return text_ids[row-2];
}
static void update(void *controller,uint32_t focus){
    void *widget=list(controller);
    uint32_t statistics=FN(STATISTICS_GET,uint32_t (*)(void *))(userdata());
    uint32_t choice=shutdown120_get(),image_row=2;
    for(uint32_t i=0;i<CHOICE_COUNT;++i)if(choices[i].choice==choice)image_row=2+i;
    for(uint32_t row=0;row<MENU_ROWS;++row){
        void *item=FN(LIST_ITEM,void *(*)(void *,uint32_t))(widget,row);
        FN(ITEM_ACTIVE,void (*)(void *,uint32_t))(item,row<2 ? row==statistics : row==image_row);
        FN(ITEM_LABEL,void (*)(void *,uint32_t))(item,label(row));
    }
    if(focus)FN(LIST_FOCUS,void (*)(void *,uint32_t))(widget,image_row);
}
void shutdown120_menu_refresh(void *controller){update(controller,1);}
uint32_t shutdown120_menu_confirm(void *controller){
    uint32_t row=selected(controller);if(row>=MENU_ROWS)return 0;
    if(row<2)FN(STATISTICS_SET,uint32_t (*)(void *,uint32_t))(userdata(),row);
    else {shutdown120_set(choices[row-2].choice);shutdown120_save_global();}
    update(controller,0);return 1;
}
void shutdown120_menu_left(void *controller,uint32_t suppress_wrap){
    if(!selected(controller) && suppress_wrap)return;
    FN(LIST_MOVE,void (*)(void *,int32_t,uint32_t))(list(controller),-1,1);
}
void shutdown120_menu_right(void *controller,uint32_t suppress_wrap){
    if(selected(controller)>=MENU_ROWS-1 && suppress_wrap)return;
    FN(LIST_MOVE,void (*)(void *,int32_t,uint32_t))(list(controller),1,1);
}
void shutdown120_menu_footer(void *controller){
    void *graphic=*(void **)((uint8_t *)controller+0x164);
    uint32_t row=selected(controller),icon=row<2 ? 17+row : 0;
    if(row==2)icon=STANDARD_PREVIEW_ICON;
    if(row>=3 && row<MENU_ROWS)icon=preview_icons[row-3];
    if(icon)FN(GRAPHIC_ICON,void (*)(void *,uint32_t))(graphic,icon);
    FN(GRAPHIC_VISIBLE,void (*)(void *,uint32_t))(graphic,icon!=0);
}
