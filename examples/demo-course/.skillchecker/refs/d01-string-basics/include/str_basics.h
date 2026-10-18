#ifndef STR_BASICS_H
#define STR_BASICS_H

#include <stddef.h>

size_t str_len(const char *s);
char *str_dup(const char *s);
char **str_split(const char *s, char sep);

#endif
