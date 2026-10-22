#include "tinyxml2.h"

int main() {
    tinyxml2::XMLDocument valid;
    if (valid.Parse("<root><value>42</value></root>") != tinyxml2::XML_SUCCESS) {
        return 1;
    }

    tinyxml2::XMLDocument malformed;
    if (malformed.Parse("<root><value></root>") == tinyxml2::XML_SUCCESS) {
        return 2;
    }

    return 0;
}
