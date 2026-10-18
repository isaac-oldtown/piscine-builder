#include <stdlib.h>
#include "str_basics.h"

size_t str_len(const char *s)
{
    size_t n = 0;
    while (s[n])
        n++;
    return n;
}

char *str_dup(const char *s)
{
    size_t n = str_len(s);
    char *p = malloc(n + 1);
    if (!p)
        return NULL;
    for (size_t i = 0; i <= n; i++)
        p[i] = s[i];
    return p;
}

static char *word(const char *s, size_t n)
{
    char *w = malloc(n + 1);
    if (!w)
        return NULL;
    for (size_t i = 0; i < n; i++)
        w[i] = s[i];
    w[n] = 0;
    return w;
}

char **str_split(const char *s, char sep)
{
    size_t count = 0;
    for (size_t i = 0; s[i];) {
        if (s[i] == sep) {
            i++;
            continue;
        }
        count++;
        while (s[i] && s[i] != sep)
            i++;
    }
    char **out = malloc((count + 1) * sizeof *out);
    if (!out)
        return NULL;
    size_t k = 0;
    for (size_t i = 0; s[i];) {
        if (s[i] == sep) {
            i++;
            continue;
        }
        size_t start = i;
        while (s[i] && s[i] != sep)
            i++;
        out[k] = word(s + start, i - start);
        if (!out[k]) {
            while (k > 0)
                free(out[--k]);
            free(out);
            return NULL;
        }
        k++;
    }
    out[k] = NULL;
    return out;
}
