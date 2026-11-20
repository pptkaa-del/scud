#ifndef PROTOCOL_STRUCTURES_H
#define PROTOCOL_STRUCTURES_H

#include <cstdint>

#pragma pack(push, 1)

/* ============================== Êîìàíäû ============================== */
typedef enum command_e {
    CMD_NONE          = 0,
    CMD_RELAY_ON      = 1,
    CMD_RELAY_OFF     = 2,
    CMD_RELAY_REBOOT  = 3,
    CMD_SEND_STATUS   = 4,
    CMD_LOCK_OPEN     = 5,
    CMD_LOCK_CLOSE    = 6
} command_e;

/* ========================== Ñîñòîÿíèå ðåëå =========================== */
typedef enum relay_state_e {
    RELAY_OFF   = 0,
    RELAY_ON    = 1,
    RELAY_ERROR = 2
} relay_state_e;

/* ========================= Ñòàòóñ óñòðîéñòâà ========================= */
typedef enum device_status_e {
    DEVICE_OFFLINE = 0,
    DEVICE_ONLINE  = 1,
    DEVICE_ERROR   = 2
} device_status_e;

/* ========================= Òèï òðåâîãè =============================== */
typedef enum alarm_type_e {
    ALARM_NONE        = 0,
    ALARM_BATTERY     = 1
} alarm_type_e;

/* ==================== Ðåçóëüòàò âûïîëíåíèÿ êîìàíäû =================== */
typedef enum rep_result_e {
    REP_SUCCESS      = 1,
    REP_FAILURE      = 2,
    REP_NOT_SUPPORTED = 3
} rep_result_e;

/* ======================= Âåðñèÿ ïðîòîêîëà ============================ */
typedef enum protocol_version_e {
    PROTOCOL_V1 = 1,
    PROTOCOL_V2 = 2
} protocol_version_e;

/* ======================= Óïðàâëÿþùèé óðîâåíü ========================= */
typedef enum control_level_e {
    CTRL_LOW  = 0,
    CTRL_HIGH = 1
} control_level_e;

/* ====================== Ñîñòîÿíèå äâåðè (v2) ======================== */
typedef enum door_state_e {
    DOOR_UNDEFINED = 0,
    DOOR_OPEN      = 1,
    DOOR_CLOSED    = 2
} door_state_e;

/* ====================== Èñòî÷íèê ïèòàíèÿ (v2) ======================= */
typedef enum power_source_e {
    POWER_BATTERY = 0,
    POWER_MAINS   = 1
} power_source_e;

/* ======================== Òèï ñåòè (v2) ============================= */
typedef enum net_type_e {
    NET_UNDEFINED = 0,
    NET_ETHERNET  = 1,
    NET_GSM       = 2
} net_type_e;

/* ======================== Ñòðóêòóðû ïàêåòîâ ========================== */
/* Òîïèê com/{id} – 12 áàéò */
typedef struct com_payload_t {
    uint32_t cmd_id;
    uint32_t command;   /* command_e */
    uint32_t data;      /* íîìåð ðåëå è ò.ï. */
} com_payload_t;

/* Òîïèê stat, âåðñèÿ 1 – 28 áàéò ïîëåçíîé íàãðóçêè áåç id/version */
typedef struct stat_v1_payload_t {
    uint32_t r1;        /* relay_state_e */
    uint32_t r2;
    uint32_t r3;
    uint32_t r4;
    uint32_t stat;      /* device_status_e */
    uint32_t sl;        /* óðîâåíü ñèãíàëà */
    uint32_t alarm;     /* alarm_type_e */
} stat_v1_payload_t;

/* Òîïèê stat, âåðñèÿ 2 */
typedef struct stat_v2_payload_t {
    uint32_t door; /* door_state_e */
    uint32_t power; /* power_source_e */
    uint32_t battery;
    uint32_t net_type; /* net_type_e */
    uint32_t relay_en;
    uint32_t r1;
    uint32_t r2;
    uint32_t r3;
    uint32_t r4;
    uint32_t stat;
    uint32_t sl;
} stat_v2_payload_t;

typedef union stat_payload_variable_u {
    stat_v1_payload_t v1;
    stat_v2_payload_t v2;
} stat_payload_variable_u;

typedef struct stat_payload_t {
    uint32_t id;
    uint32_t version;   /* protocol_version_e */
    stat_payload_variable_u payload;
} stat_payload_t;

/* Òîïèê rep – 12 áàéò */
typedef struct rep_payload_t {
    uint32_t id;
    uint32_t cmd_id;
    uint32_t success;   /* rep_result_e */
} rep_payload_t;

/* Òîïèê photos – çàãîëîâîê 20 áàéò, äàëåå photo_size áàéò äàííûõ */
typedef struct photos_header_t {
    uint32_t id;
    uint32_t event_id;
    uint32_t photos_cnt;
    uint32_t photo_id;
    uint32_t photo_size;
} photos_header_t;

#pragma pack(pop)

/* Ïðîâåðêè ðàçìåðîâ */
static_assert(sizeof(com_payload_t) == 12, "com_payload_t size must be 12");
static_assert(sizeof(stat_v1_payload_t) == 28, "stat_v1_payload_t size must be 28");
static_assert(sizeof(stat_v2_payload_t) == 44, "stat_v2_payload_t size must be 44");
static_assert(sizeof(stat_payload_t) == 52, "stat_payload_t size must be 52");
static_assert(sizeof(rep_payload_t) == 12, "rep_payload_t size must be 12");
static_assert(sizeof(photos_header_t) == 20, "photos_header_t size must be 20");

#endif // PROTOCOL_STRUCTURES_H
